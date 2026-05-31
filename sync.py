#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox for new emails and syncs sender contacts to HubSpot.

Usage:
  python sync.py           # continuous polling (default: every 60 s)
  python sync.py --once    # single run and exit
  python sync.py --interval 300  # poll every 5 minutes
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ──────────────────────────────────────────────
# Lazy imports (only fail at runtime if missing)
# ──────────────────────────────────────────────

def _gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = None
    token_path = Path(os.getenv("GMAIL_TOKEN_PATH", "state/gmail_token.json"))
    creds_path = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials/gmail_credentials.json"))

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _hubspot_client():
    from hubspot import HubSpot
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN is not set.")
    return HubSpot(access_token=token)


# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

@dataclass
class SenderInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    domain: str = ""
    company: str = ""
    raw_name: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(filter(None, [self.first_name, self.last_name]))


@dataclass
class SyncResult:
    status: str          # "CREATED" | "UPDATED" | "IGNORED"
    email: str
    contact_id: str = ""
    reason: str = ""


# ──────────────────────────────────────────────
# State (tracks processed message IDs)
# ──────────────────────────────────────────────

STATE_PATH = Path(os.getenv("SYNC_STATE_PATH", "state/sync_state.json"))

_SKIP_DOMAINS = {
    "googlemail.com", "mailer-daemon.googlemail.com",
    "bounce.google.com", "noreply.google.com",
}
_SKIP_PREFIXES = ("mailer-daemon", "noreply", "no-reply", "postmaster", "bounce")


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"processed_ids": [], "last_history_id": None}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


# ──────────────────────────────────────────────
# Email parsing helpers
# ──────────────────────────────────────────────

_FROM_HEADER_RE = re.compile(r'^"?([^"<]+?)"?\s*<([^>]+)>$|^([^\s@]+@[^\s]+)$')
_FORWARDED_RE = re.compile(
    r'Da\s+"([^"]+)"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)

# Known free/generic domains whose name shouldn't be used as company
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it", "live.com", "live.it", "libero.it",
    "tiscali.it", "virgilio.it", "icloud.com", "me.com",
}


def _parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (first, last)."""
    parts = display_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> str:
    """Best-effort: turn a domain into a human-readable company name."""
    if not domain or domain in _GENERIC_DOMAINS:
        return ""
    # strip TLD and www, capitalise
    base = re.sub(r"\.[a-z]{2,6}$", "", domain.lower())
    base = re.sub(r"^www\.", "", base)
    return base.replace("-", " ").replace(".", " ").title()


def parse_sender(from_header: str, body_snippet: str = "") -> Optional[SenderInfo]:
    """Extract a SenderInfo from the From: header (and optionally the body)."""
    from_header = from_header.strip()
    m = _FROM_HEADER_RE.match(from_header)
    if not m:
        return None

    if m.group(1) and m.group(2):
        raw_name, email = m.group(1).strip(), m.group(2).strip().lower()
    else:
        raw_name, email = "", m.group(3).strip().lower()

    # Skip noisy/system senders
    local = email.split("@")[0]
    domain = email.split("@")[1] if "@" in email else ""

    if domain in _SKIP_DOMAINS or any(local.startswith(p) for p in _SKIP_PREFIXES):
        return None

    first, last = _parse_name(raw_name) if raw_name else ("", "")
    company = _domain_to_company(domain)

    sender = SenderInfo(
        email=email,
        first_name=first,
        last_name=last,
        domain=domain,
        company=company,
        raw_name=raw_name,
    )

    # If the email is a forwarding relay (redazione@…), look into the snippet
    # for the real "Da: Name <email>" block
    if body_snippet:
        fw = _FORWARDED_RE.search(body_snippet)
        if fw:
            fw_name, fw_email = fw.group(1).strip(), fw.group(2).strip().lower()
            fw_local = fw_email.split("@")[0]
            fw_domain = fw_email.split("@")[1] if "@" in fw_email else ""
            if fw_domain not in _SKIP_DOMAINS and not any(
                fw_local.startswith(p) for p in _SKIP_PREFIXES
            ):
                f, l = _parse_name(fw_name)
                sender = SenderInfo(
                    email=fw_email,
                    first_name=f,
                    last_name=l,
                    domain=fw_domain,
                    company=_domain_to_company(fw_domain),
                    raw_name=fw_name,
                )

    return sender


# ──────────────────────────────────────────────
# HubSpot operations
# ──────────────────────────────────────────────

_HS_TAG = "Inbound Gmail"


def find_contact(email: str, hs) -> Optional[dict]:
    from hubspot.crm.contacts import ApiException

    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": [
                    "email", "firstname", "lastname", "company",
                    "hs_lead_status", "hs_analytics_source_data_1",
                ],
                "limit": 1,
            }
        )
        return resp.results[0].to_dict() if resp.results else None
    except ApiException as exc:
        logging.error("HubSpot search failed for %s: %s", email, exc)
        return None


def _build_properties(sender: SenderInfo, existing: Optional[dict] = None) -> dict:
    """Build the HubSpot property map, filling only missing/blank fields."""
    props: dict = {}
    ex_props = existing.get("properties", {}) if existing else {}

    def _set(key: str, value: str) -> None:
        if value and not ex_props.get(key):
            props[key] = value

    _set("email", sender.email)
    _set("firstname", sender.first_name)
    _set("lastname", sender.last_name)
    _set("company", sender.company)

    # Lead source / contact source
    if not ex_props.get("hs_analytics_source_data_1"):
        props["hs_analytics_source_data_1"] = "Gmail"

    return props


def create_contact(sender: SenderInfo, hs) -> SyncResult:
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException

    props = {
        "email": sender.email,
        "firstname": sender.first_name,
        "lastname": sender.last_name,
        "company": sender.company,
        "hs_analytics_source_data_1": "Gmail",
    }
    # Remove blank values
    props = {k: v for k, v in props.items() if v}

    try:
        resp = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        contact_id = resp.id
        _add_note(contact_id, sender, hs)
        logging.info("CREATED  %s → HubSpot ID %s", sender.email, contact_id)
        return SyncResult(status="CREATED", email=sender.email, contact_id=contact_id)
    except ApiException as exc:
        if exc.status == 409:
            # Race condition: contact created between search and create
            body = json.loads(exc.body) if exc.body else {}
            contact_id = body.get("message", "").split(":")[-1].strip()
            return SyncResult(
                status="IGNORED",
                email=sender.email,
                contact_id=contact_id,
                reason="duplicate (race)",
            )
        logging.error("HubSpot create failed for %s: %s", sender.email, exc)
        return SyncResult(
            status="IGNORED", email=sender.email, reason=f"create error: {exc.status}"
        )


def update_contact(contact_id: str, sender: SenderInfo, existing: dict, hs) -> SyncResult:
    from hubspot.crm.contacts import SimplePublicObjectInput, ApiException

    props = _build_properties(sender, existing)
    if not props:
        logging.info("IGNORED  %s (no new data)", sender.email)
        return SyncResult(
            status="IGNORED",
            email=sender.email,
            contact_id=contact_id,
            reason="no new fields",
        )

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        _add_note(contact_id, sender, hs)
        logging.info("UPDATED  %s → HubSpot ID %s", sender.email, contact_id)
        return SyncResult(status="UPDATED", email=sender.email, contact_id=contact_id)
    except ApiException as exc:
        logging.error("HubSpot update failed for %s: %s", sender.email, exc)
        return SyncResult(
            status="IGNORED", email=sender.email, contact_id=contact_id,
            reason=f"update error: {exc.status}",
        )


def _add_note(contact_id: str, sender: SenderInfo, hs) -> None:
    """Log a timeline note: email received via Gmail."""
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate
    from hubspot.crm.associations import BatchInputPublicAssociation, PublicAssociation

    body = (
        f"Email ricevuta via Gmail da {sender.full_name or sender.email} "
        f"({sender.email}). Tag: {_HS_TAG}."
    )
    timestamp = str(int(time.time() * 1000))
    try:
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": timestamp}
            )
        )
        # Associate note → contact
        hs.crm.associations.batch_api.create(
            from_object_type="notes",
            to_object_type="contacts",
            batch_input_public_association=BatchInputPublicAssociation(
                inputs=[
                    PublicAssociation(
                        from_={"id": note.id},
                        to={"id": contact_id},
                        type="note_to_contact",
                    )
                ]
            ),
        )
    except Exception as exc:
        logging.debug("Note creation skipped: %s", exc)


def sync_contact(sender: SenderInfo, hs) -> SyncResult:
    existing = find_contact(sender.email, hs)
    if existing:
        return update_contact(existing["id"], sender, existing, hs)
    return create_contact(sender, hs)


# ──────────────────────────────────────────────
# Gmail polling
# ──────────────────────────────────────────────

def fetch_new_messages(gmail, state: dict) -> tuple[list[dict], str | None]:
    """
    Return (messages, new_history_id).

    Uses Gmail History API if we have a historyId; otherwise falls back to
    a fresh search of recent inbox messages.
    """
    history_id = state.get("last_history_id")
    processed = set(state.get("processed_ids", []))

    messages = []
    new_history_id = history_id

    if history_id:
        try:
            resp = (
                gmail.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            new_history_id = resp.get("historyId", history_id)
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added.get("message", {})
                    mid = msg.get("id")
                    if mid and mid not in processed:
                        messages.append(msg)
        except Exception as exc:
            logging.warning("History API error (%s); falling back to search.", exc)
            history_id = None  # force fallback

    if not history_id:
        resp = (
            gmail.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=50)
            .execute()
        )
        new_history_id = None  # reset; will be captured from first message
        for msg in resp.get("messages", []):
            mid = msg.get("id")
            if mid and mid not in processed:
                messages.append(msg)

    return messages, new_history_id


def get_message_detail(gmail, msg_id: str) -> dict:
    return (
        gmail.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata",
             metadataHeaders=["From", "Subject"])
        .execute()
    )


# ──────────────────────────────────────────────
# Main sync loop
# ──────────────────────────────────────────────

def run_once(gmail, hs, state: dict) -> list[SyncResult]:
    results: list[SyncResult] = []
    messages, new_history_id = fetch_new_messages(gmail, state)

    if not messages:
        logging.info("No new messages.")
        if new_history_id:
            state["last_history_id"] = new_history_id
        return results

    logging.info("Processing %d new message(s)…", len(messages))
    processed_ids: list[str] = state.setdefault("processed_ids", [])

    for msg in messages:
        mid = msg.get("id")
        if not mid:
            continue

        detail = get_message_detail(gmail, mid)

        # Capture historyId from the first real message
        if not state.get("last_history_id"):
            state["last_history_id"] = detail.get("historyId")

        headers = {
            h["name"].lower(): h["value"]
            for h in detail.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("from", "")
        snippet = detail.get("snippet", "")

        sender = parse_sender(from_header, snippet)
        if not sender:
            logging.debug("Skipping message %s (sender filtered)", mid)
            processed_ids.append(mid)
            continue

        result = sync_contact(sender, hs)
        results.append(result)
        processed_ids.append(mid)
        print(
            f"  [{result.status:8s}]  {result.email:<40s}  "
            f"ID: {result.contact_id or '-'}"
            + (f"  ({result.reason})" if result.reason else "")
        )

    if new_history_id:
        state["last_history_id"] = new_history_id

    # Keep only the last 2000 processed IDs to avoid unbounded growth
    state["processed_ids"] = processed_ids[-2000:]
    save_state(state)
    return results


def main() -> None:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument(
        "--interval", type=int, default=60, help="Polling interval in seconds (default: 60)"
    )
    args = parser.parse_args()

    gmail = _gmail_service()
    hs = _hubspot_client()
    state = load_state()

    if args.once:
        run_once(gmail, hs, state)
        return

    logging.info("Starting continuous sync (interval: %ds). Ctrl+C to stop.", args.interval)
    try:
        while True:
            try:
                run_once(gmail, hs, state)
            except Exception as exc:
                logging.error("Sync cycle error: %s", exc, exc_info=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        logging.info("Stopped.")


if __name__ == "__main__":
    main()
