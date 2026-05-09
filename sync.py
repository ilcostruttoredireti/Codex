#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.

For each new email it:
  1. Extracts the real sender (parsing forwarded-email bodies when needed)
  2. Searches HubSpot by email as unique key
  3. Creates a new contact OR updates only missing fields on an existing one
  4. Logs a NOTE on the contact timeline: "Inbound Gmail"

Output per processed email:
  [Creato | Aggiornato | Ignorato]  email  HubSpot-ID

Usage:
  python sync.py

Requirements:
  1. credentials.json  — Google OAuth2 client-secret file (Desktop App type)
  2. .env              — HUBSPOT_ACCESS_TOKEN and optional settings
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
MAX_INBOX_SCAN = int(os.getenv("MAX_INBOX_SCAN", "50"))

# Addresses that are internal forwarding relays — never sync as contacts
_internal_raw = os.getenv("INTERNAL_SENDERS", "")
INTERNAL_SENDERS: set[str] = {
    e.strip().lower() for e in _internal_raw.split(",") if e.strip()
}

# Local parts that belong to system/bot senders — always skip
SKIP_LOCAL_PARTS = frozenset({
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "notifications", "support",
    "info",   # generic info@ could be a real contact — remove if desired
})

# ── Regex ──────────────────────────────────────────────────────────────────────
_RE_NAME_EMAIL = re.compile(r'^"?([^"<]+?)"?\s*<([^>]+)>$')
_RE_EMAIL_ONLY = re.compile(r'[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}')

# Italian/English forwarded header:  Da "Nome" email   or   From "Name" email
_RE_FWD_QUOTED = re.compile(
    r'(?:^|\n)(?:Da|From)\s+"([^"]+)"\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})',
    re.IGNORECASE,
)
# Without quotes:  Da Marco press@example.com
_RE_FWD_PLAIN = re.compile(
    r'(?:^|\n)(?:Da|From)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]{1,40}?)\s+([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s*(?:\n|$)',
    re.IGNORECASE,
)


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def build_gmail_service():
    """Authenticate and return a Gmail API service object."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token_path = Path("token.json")
    creds: Optional[Credentials] = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path("credentials.json").exists():
                raise FileNotFoundError(
                    "credentials.json not found. Download it from Google Cloud Console "
                    "(APIs & Services → Credentials → OAuth 2.0 Client IDs → Desktop App)."
                )
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def build_hubspot_client():
    """Return an authenticated HubSpot client."""
    import hubspot

    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN not set. Create a Private App in HubSpot "
            "(Settings → Integrations → Private Apps) with contacts read/write scope."
        )
    return hubspot.Client.create(access_token=token)


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"processed_ids": [], "last_history_id": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ── Email parsing ──────────────────────────────────────────────────────────────

def parse_from_header(header: str) -> tuple[str, str]:
    """Parse a From header → (display_name, email_address)."""
    header = header.strip()
    m = _RE_NAME_EMAIL.match(header)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    # bare address
    emails = _RE_EMAIL_ONLY.findall(header)
    return "", emails[0].lower() if emails else ""


def extract_forwarded_sender(body: str) -> tuple[str, str]:
    """
    Detect the original sender embedded in a forwarded-email body.

    Handles Italian 'Da "Nome" email' and English 'From "Name" email' patterns.
    Returns (name, email) or ("", "") if nothing found.
    """
    m = _RE_FWD_QUOTED.search(body)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    m = _RE_FWD_PLAIN.search(body)
    if m:
        return m.group(1).strip(), m.group(2).strip().lower()
    return "", ""


def extract_plain_body(payload: dict) -> str:
    """Recursively extract text/plain content from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    for part in payload.get("parts", []):
        result = extract_plain_body(part)
        if result:
            return result
    return ""


def should_skip(email: str) -> bool:
    """Return True for addresses that should never become HubSpot contacts."""
    if not email or "@" not in email:
        return True
    local = email.split("@")[0].lower()
    return any(pat in local for pat in SKIP_LOCAL_PARTS)


def split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (full_name, "")


def domain_to_company(domain: str) -> str:
    """Derive a rough company name from the email domain."""
    stem = domain.lower().split(".")[0]
    return stem.replace("-", " ").replace("_", " ").title()


# ── HubSpot operations ─────────────────────────────────────────────────────────

def hs_find_contact(hs, email: str):
    """Search HubSpot for a contact by email. Returns the first result or None."""
    from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest

    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "lifecyclestage"],
        limit=1,
    )
    res = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    return res.results[0] if res.results else None


def hs_create_contact(hs, email: str, first: str, last: str, company: str):
    """Create a new HubSpot contact."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    props: dict[str, str] = {"email": email, "lifecyclestage": "lead"}
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company

    return hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )


def hs_update_missing_fields(
    hs, contact_id: str, existing_props: dict, first: str, last: str, company: str
) -> list[str]:
    """Patch only fields that are currently blank. Returns list of updated field names."""
    from hubspot.crm.contacts import SimplePublicObjectInput

    updates: dict[str, str] = {}
    if first and not existing_props.get("firstname"):
        updates["firstname"] = first
    if last and not existing_props.get("lastname"):
        updates["lastname"] = last
    if company and not existing_props.get("company"):
        updates["company"] = company

    if updates:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
    return list(updates.keys())


def hs_add_note(hs, contact_id: str, subject: str, sender_email: str) -> None:
    """Attach a timeline NOTE to the contact recording the inbound Gmail."""
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteCreate,
        )

        ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        body = (
            f"📧 Inbound Gmail\n"
            f"Da: {sender_email}\n"
            f"Oggetto: {subject}\n"
            f"Tag: Inbound Gmail"
        )
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(
                properties={"hs_note_body": body, "hs_timestamp": ts_ms}
            )
        )
        hs.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        log.warning("Could not add timeline note for %s: %s", contact_id, exc)


# ── Core sync ──────────────────────────────────────────────────────────────────

def sync_contact(hs, email: str, name: str, subject: str) -> dict:
    """
    Sync a single email address to HubSpot.

    Returns a result dict with keys: status, email, hubspot_id (optional),
    updated_fields (optional), reason (optional).
    """
    if email in INTERNAL_SENDERS or should_skip(email):
        return {"status": "Ignorato", "email": email, "reason": "internal/system"}

    first, last = split_name(name) if name else ("", "")
    domain = email.split("@")[1] if "@" in email else ""
    company = domain_to_company(domain)

    existing = hs_find_contact(hs, email)
    if existing:
        updated = hs_update_missing_fields(
            hs, existing.id, existing.properties, first, last, company
        )
        hs_add_note(hs, existing.id, subject, email)
        return {
            "status": "Aggiornato",
            "email": email,
            "hubspot_id": existing.id,
            "updated_fields": updated,
        }

    contact = hs_create_contact(hs, email, first, last, company)
    hs_add_note(hs, contact.id, subject, email)
    return {"status": "Creato", "email": email, "hubspot_id": contact.id}


def process_message(gmail, hs, msg_id: str) -> list[dict]:
    """
    Fetch one Gmail message, extract real sender(s), sync each to HubSpot.

    For forwarded emails (Fw:/Fwd: subject) the original sender is parsed
    from the body in addition to the envelope From address.
    """
    msg = gmail.users().messages().get(
        userId="me", id=msg_id, format="full"
    ).execute()

    payload = msg.get("payload", {})
    headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")
    is_fwd = re.match(r"^(?:fw|fwd)\s*:", subject, re.IGNORECASE) is not None

    results: list[dict] = []
    seen_emails: set[str] = set()

    def _maybe_sync(email: str, name: str) -> None:
        if email and email not in seen_emails:
            seen_emails.add(email)
            results.append(sync_contact(hs, email, name, subject))

    # 1. If forwarded: extract original sender from body
    if is_fwd:
        body = extract_plain_body(payload)
        if body:
            fwd_name, fwd_email = extract_forwarded_sender(body)
            _maybe_sync(fwd_email, fwd_name)

    # 2. Envelope sender (always attempt, skip if internal/system)
    direct_name, direct_email = parse_from_header(from_header)
    _maybe_sync(direct_email, direct_name)

    if not results:
        results.append({"status": "Ignorato", "email": "", "reason": "no valid sender"})

    return results


# ── Gmail polling ──────────────────────────────────────────────────────────────

def fetch_new_message_ids(gmail, last_history_id: Optional[str]) -> tuple[list[str], str]:
    """
    Return (list_of_new_message_ids, new_history_id).

    Uses the efficient History API when a previous history_id is known;
    falls back to a full inbox scan on first run or after history expiry.
    """
    if last_history_id:
        try:
            resp = gmail.users().history().list(
                userId="me",
                startHistoryId=last_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            ).execute()
            ids = [
                added["message"]["id"]
                for record in resp.get("history", [])
                for added in record.get("messagesAdded", [])
            ]
            return ids, resp.get("historyId", last_history_id)
        except Exception as exc:
            log.warning("History API error (%s) — falling back to full scan", exc)

    # First run or expired history: scan inbox
    resp = gmail.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=MAX_INBOX_SCAN
    ).execute()
    ids = [m["id"] for m in resp.get("messages", [])]
    profile = gmail.users().getProfile(userId="me").execute()
    return ids, profile.get("historyId", "")


# ── Main loop ──────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("Gmail → HubSpot contact sync — started")
    log.info("Poll interval: %ds  |  Internal senders: %s", POLL_INTERVAL, INTERNAL_SENDERS or "{none}")
    log.info("=" * 60)

    gmail = build_gmail_service()
    hs = build_hubspot_client()
    state = load_state()

    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}

    while True:
        try:
            log.info("Checking for new emails…")
            new_ids, new_history_id = fetch_new_message_ids(
                gmail, state.get("last_history_id")
            )

            already_done = set(state["processed_ids"])
            to_process = [mid for mid in new_ids if mid not in already_done]

            if to_process:
                log.info("Found %d new message(s) to process", len(to_process))

            for msg_id in to_process:
                try:
                    results = process_message(gmail, hs, msg_id)
                    for r in results:
                        status = r.get("status", "?")
                        email = r.get("email", "—")
                        hs_id = r.get("hubspot_id", "N/A")
                        totals[status] = totals.get(status, 0) + 1
                        log.info(
                            "[%-10s]  %-40s  HubSpot ID: %s",
                            status, email, hs_id,
                        )
                    state["processed_ids"].append(msg_id)
                except Exception as exc:
                    log.error("Error processing message %s: %s", msg_id, exc, exc_info=True)

            # Keep state file bounded
            if len(state["processed_ids"]) > 10_000:
                state["processed_ids"] = state["processed_ids"][-5_000:]

            state["last_history_id"] = new_history_id
            save_state(state)

            if to_process:
                log.info(
                    "Session totals — Creato: %d  Aggiornato: %d  Ignorato: %d",
                    totals["Creato"], totals["Aggiornato"], totals["Ignorato"],
                )

        except KeyboardInterrupt:
            log.info("Interrupted by user — exiting.")
            save_state(state)
            break
        except Exception as exc:
            log.error("Main loop error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
