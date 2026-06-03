#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitors the Gmail inbox for new inbound emails and syncs senders
to HubSpot as contacts (create or update), avoiding duplicates.

Usage:
    python gmail_hubspot_sync.py           # single run
    python gmail_hubspot_sync.py --daemon  # continuous loop (every POLL_INTERVAL_SEC)

Required env vars:
    HUBSPOT_TOKEN   – HubSpot private-app token
    OWN_EMAIL       – your own Gmail address (skipped as sender)

Optional env vars:
    POLL_INTERVAL_SEC  – seconds between loops in daemon mode (default: 300)
    LOG_LEVEL          – DEBUG / INFO / WARNING (default: INFO)
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Constants ──────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path("gmail_credentials.json")
TOKEN_FILE = Path("gmail_token.json")
STATE_FILE = Path("sync_state.json")

HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_TOKEN", "")
OWN_EMAIL = os.environ.get("OWN_EMAIL", "").lower()
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SEC", "300"))

# Automated senders to ignore (local-part prefix or domain prefix)
_SKIP_LOCAL = frozenset(
    {
        "noreply",
        "no-reply",
        "donotreply",
        "do-not-reply",
        "mailer-daemon",
        "postmaster",
        "bounce",
        "bounces",
        "notifications",
        "newsletter",
        "alerts",
        "support",
        "info",
        "feedback",
    }
)
# Public free-email domains whose domain name is not a meaningful company name
_PUBLIC_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.it",
        "hotmail.com",
        "hotmail.it",
        "outlook.com",
        "outlook.it",
        "live.com",
        "live.it",
        "icloud.com",
        "me.com",
        "mac.com",
        "libero.it",
        "alice.it",
        "virgilio.it",
        "tin.it",
        "tiscali.it",
        "email.it",
        "fastwebnet.it",
        "protonmail.com",
        "proton.me",
    }
)

# ── Logging ────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


# ── Gmail helpers ──────────────────────────────────────────────────────────────


def build_gmail_service():
    """Authenticate with OAuth2 and return a Gmail API service object."""
    creds: Credentials | None = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"{CREDENTIALS_FILE} not found. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_inbox_messages(service, after_epoch: int | None) -> list[dict]:
    """Return Gmail message stubs (id + threadId) for inbox emails since *after_epoch*."""
    query = "in:inbox -in:sent -in:draft"
    if after_epoch:
        query += f" after:{after_epoch}"

    messages: list[dict] = []
    page_token = None
    while True:
        kwargs: dict = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_sender_from_message(service, message_id: str) -> tuple[str, str] | None:
    """Return (email, display_name) for the message sender, or None."""
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From"],
        )
        .execute()
    )
    headers = {
        h["name"].lower(): h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    return _parse_from_header(headers.get("from", ""))


def _parse_from_header(header: str) -> tuple[str, str] | None:
    """Parse 'Display Name <user@domain.com>' or bare address into (email, name)."""
    header = header.strip()
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', header)
    if m:
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
    else:
        email = header.lower()
        name = ""
    if "@" not in email:
        return None
    return email, name


# ── HubSpot helpers ────────────────────────────────────────────────────────────


def _hs_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _hs_get(path: str, params: dict | None = None) -> dict:
    r = requests.get(
        f"{HUBSPOT_API_BASE}{path}", headers=_hs_headers(), params=params or {}
    )
    r.raise_for_status()
    return r.json()


def _hs_post(path: str, body: dict) -> dict:
    r = requests.post(f"{HUBSPOT_API_BASE}{path}", headers=_hs_headers(), json=body)
    r.raise_for_status()
    return r.json()


def _hs_patch(path: str, body: dict) -> dict:
    r = requests.patch(f"{HUBSPOT_API_BASE}{path}", headers=_hs_headers(), json=body)
    r.raise_for_status()
    return r.json()


def find_contact_by_email(email: str) -> dict | None:
    """Search HubSpot for a contact with the given email. Returns the record or None."""
    data = _hs_post(
        "/crm/v3/objects/contacts/search",
        {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": [
                "email",
                "firstname",
                "lastname",
                "company",
                "hs_lead_source",
            ],
            "limit": 1,
        },
    )
    results = data.get("results", [])
    return results[0] if results else None


def _name_parts(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


def _company_from_email(email: str) -> str:
    """Derive a company name from the email domain when it is not a public provider."""
    domain = email.split("@", 1)[1].lower()
    if domain in _PUBLIC_DOMAINS:
        return ""
    # Take first segment of domain, capitalise nicely
    name = domain.split(".")[0].replace("-", " ").title()
    return name


def create_contact(email: str, display_name: str) -> dict:
    firstname, lastname = _name_parts(display_name)
    company = _company_from_email(email)
    props: dict[str, str] = {
        "email": email,
        "hs_lead_source": "Gmail",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return _hs_post("/crm/v3/objects/contacts", {"properties": props})


def update_contact_missing_fields(
    contact_id: str, email: str, display_name: str, existing_props: dict
) -> dict:
    """Patch a contact, filling only blank fields. Returns patched record or marker dict."""
    firstname, lastname = _name_parts(display_name)
    company = _company_from_email(email)
    updates: dict[str, str] = {}

    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return {"id": contact_id, "_unchanged": True}
    return _hs_patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})


def add_inbound_email_activity(contact_id: str, sender_email: str) -> None:
    """Record an inbound email engagement on the contact's timeline (best-effort)."""
    try:
        _hs_post(
            "/engagements/v1/engagements",
            {
                "engagement": {"active": True, "type": "EMAIL"},
                "associations": {"contactIds": [int(contact_id)]},
                "metadata": {
                    "from": {"email": sender_email},
                    "subject": "Inbound Gmail",
                    "direction": "INBOUND",
                },
            },
        )
    except Exception as exc:
        log.debug("Timeline activity skipped for %s: %s", contact_id, exc)


# ── Filter helpers ─────────────────────────────────────────────────────────────


def is_automated_address(email: str) -> bool:
    """Return True if the address looks like an automated/system sender."""
    local = email.split("@")[0]
    for skip in _SKIP_LOCAL:
        if local == skip or local.startswith(skip + "+") or local.startswith(skip + "."):
            return True
    return False


# ── State persistence ──────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_sync_epoch": None, "processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Core sync ──────────────────────────────────────────────────────────────────


def run_once(service) -> list[dict]:
    """
    Execute one full sync cycle.

    Returns a list of result dicts:
        {"status": "Creato"|"Aggiornato"|"Ignorato", "email": str, "hubspot_id": str}
    """
    state = load_state()
    after_epoch: int | None = state.get("last_sync_epoch")
    processed_ids: set[str] = set(state.get("processed_ids", []))
    cycle_start = int(time.time())

    log.info(
        "Starting sync cycle%s",
        f" (since {datetime.fromtimestamp(after_epoch).isoformat()})" if after_epoch else "",
    )
    messages = fetch_inbox_messages(service, after_epoch)
    new_messages = [m for m in messages if m["id"] not in processed_ids]
    log.info("%d total messages fetched, %d new to process", len(messages), len(new_messages))

    results: list[dict] = []

    for msg in new_messages:
        msg_id = msg["id"]
        processed_ids.add(msg_id)

        try:
            sender = get_sender_from_message(service, msg_id)
        except Exception as exc:
            log.warning("Could not read message %s: %s", msg_id, exc)
            continue

        if not sender:
            continue

        email, display_name = sender

        # Skip own address
        if OWN_EMAIL and email == OWN_EMAIL:
            log.debug("Skipping own address: %s", email)
            continue

        # Skip automated senders
        if is_automated_address(email):
            log.debug("Skipping automated address: %s", email)
            continue

        try:
            existing = find_contact_by_email(email)

            if existing:
                contact_id = existing["id"]
                patch = update_contact_missing_fields(
                    contact_id, email, display_name, existing.get("properties", {})
                )
                if patch.get("_unchanged"):
                    status = "Ignorato"
                else:
                    status = "Aggiornato"
            else:
                created = create_contact(email, display_name)
                contact_id = created["id"]
                status = "Creato"

            if status in ("Creato", "Aggiornato"):
                add_inbound_email_activity(contact_id, email)

            entry = {"status": status, "email": email, "hubspot_id": contact_id}
            results.append(entry)
            log.info("[%s]  %-40s  HubSpot ID: %s", status, email, contact_id)

        except requests.HTTPError as exc:
            log.error("HubSpot error for %s: %s", email, exc)
        except Exception as exc:
            log.exception("Unexpected error for %s: %s", email, exc)

    # Keep processed_ids bounded to avoid unbounded state file growth
    if len(processed_ids) > 10_000:
        processed_ids = set(list(processed_ids)[-10_000:])

    state["last_sync_epoch"] = cycle_start
    state["processed_ids"] = list(processed_ids)
    save_state(state)

    # Summary
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = " | ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
    log.info("Cycle complete — %s", summary or "nothing to do")

    return results


def print_results_table(results: list[dict]) -> None:
    if not results:
        return
    print("\n" + "─" * 72)
    print(f"{'STATUS':<12} {'EMAIL':<42} {'HUBSPOT ID'}")
    print("─" * 72)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<42} {r['hubspot_id']}")
    print("─" * 72 + "\n")


# ── Entry point ────────────────────────────────────────────────────────────────


def main() -> None:
    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_TOKEN environment variable is not set.")
        sys.exit(1)

    daemon_mode = "--daemon" in sys.argv

    service = build_gmail_service()

    if daemon_mode:
        log.info("Daemon mode active — polling every %d seconds", POLL_INTERVAL)
        while True:
            try:
                results = run_once(service)
                print_results_table(results)
            except Exception as exc:
                log.exception("Unhandled error in sync cycle: %s", exc)
            log.info("Next check in %d seconds…", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)
    else:
        results = run_once(service)
        print_results_table(results)


if __name__ == "__main__":
    main()
