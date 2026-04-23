#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail for new emails and syncs senders as contacts in HubSpot.
"""

import os
import json
import time
import base64
import logging
import re
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Configuration ────────────────────────────────────────────────────────────

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path("credentials.json")
STATE_FILE = Path(".sync_state.json")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL", "60"))
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    "credentials.json not found. Download it from Google Cloud Console "
                    "(APIs & Services → Credentials → OAuth 2.0 Client IDs)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_history_id": None, "processed_message_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_new_messages(service, state: dict) -> list[dict]:
    """Return list of new inbox messages since the last sync."""
    profile = service.users().getProfile(userId="me").execute()
    current_history_id = profile["historyId"]

    if state["last_history_id"] is None:
        # First run: fetch the 50 most recent inbox messages
        log.info("First run — fetching last 50 inbox messages.")
        result = service.users().messages().list(
            userId="me", labelIds=["INBOX"], maxResults=50
        ).execute()
        messages = result.get("messages", [])
        state["last_history_id"] = current_history_id
        save_state(state)
        return messages

    try:
        history = service.users().history().list(
            userId="me",
            startHistoryId=state["last_history_id"],
            historyTypes=["messageAdded"],
            labelId="INBOX",
        ).execute()
        messages = []
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                messages.append(added["message"])
        state["last_history_id"] = current_history_id
        save_state(state)
        return messages
    except Exception as exc:
        log.warning("History fetch failed (%s). Resetting history ID.", exc)
        state["last_history_id"] = current_history_id
        save_state(state)
        return []


def get_message_sender(service, msg_id: str) -> dict | None:
    """Fetch message metadata and extract sender fields."""
    try:
        msg = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
    except Exception as exc:
        log.error("Could not fetch message %s: %s", msg_id, exc)
        return None

    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    display_name, email_address = parseaddr(raw_from)
    email_address = email_address.strip().lower()

    if not email_address or "@" not in email_address:
        return None

    # Skip emails from self
    domain = email_address.split("@")[1]

    first_name, last_name = _split_name(display_name)

    return {
        "email": email_address,
        "display_name": display_name.strip(),
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": _company_from_domain(domain),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "message_id": msg_id,
    }


def _split_name(display_name: str) -> tuple[str, str]:
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "tutanota.com", "gmx.com", "yandex.com", "mail.com",
}

def _company_from_domain(domain: str) -> str:
    if domain in _GENERIC_DOMAINS:
        return ""
    # Strip common subdomains and TLDs to get company name
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]  # e.g. "acme" from "mail.acme.com"
        return name.capitalize()
    return domain


# ── HubSpot helpers ───────────────────────────────────────────────────────────

HS_BASE = "https://api.hubapi.com"

def _hs_headers() -> dict:
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY environment variable is not set.")
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(email: str) -> dict | None:
    """Return existing HubSpot contact dict or None."""
    url = f"{HS_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
        }],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, headers=_hs_headers(), json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None


def create_contact(sender: dict) -> dict:
    """Create a new HubSpot contact and return the created object."""
    url = f"{HS_BASE}/crm/v3/objects/contacts"
    properties = _build_properties(sender, is_new=True)
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, headers=_hs_headers(), json={"properties": properties})
        resp.raise_for_status()
        return resp.json()


def update_contact(contact_id: str, sender: dict, existing: dict) -> dict:
    """Patch only the missing/empty fields on an existing contact."""
    url = f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}"
    existing_props = existing.get("properties", {})
    updates = {}

    field_map = {
        "firstname": sender["first_name"],
        "lastname": sender["last_name"],
        "company": sender["company"],
    }
    for prop, value in field_map.items():
        if value and not existing_props.get(prop):
            updates[prop] = value

    # Always ensure source tag is set
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = CONTACT_SOURCE

    if not updates:
        return existing  # nothing to patch

    with httpx.Client(timeout=15) as client:
        resp = client.patch(url, headers=_hs_headers(), json={"properties": updates})
        resp.raise_for_status()
        return resp.json()


def add_note_to_contact(contact_id: str, sender: dict):
    """Create a NOTE activity on the contact recording the inbound email."""
    url = f"{HS_BASE}/crm/v3/objects/notes"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    note_body = (
        f"Inbound email received\n"
        f"From: {sender['display_name']} <{sender['email']}>\n"
        f"Subject: {sender['subject']}\n"
        f"Date: {sender['date']}\n"
        f"Tag: {CONTACT_TAG}"
    )
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": str(now_ms),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }],
    }
    with httpx.Client(timeout=15) as client:
        resp = client.post(url, headers=_hs_headers(), json=payload)
        if resp.status_code not in (200, 201):
            log.warning("Note creation failed for contact %s: %s", contact_id, resp.text)


def _build_properties(sender: dict, is_new: bool) -> dict:
    props: dict = {"email": sender["email"], "hs_lead_source": CONTACT_SOURCE}
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    return props


# ── Core sync logic ───────────────────────────────────────────────────────────

def process_sender(sender: dict) -> dict:
    """
    Check HubSpot for the sender and create/update accordingly.
    Returns a result dict with status, email, and contact_id.
    """
    email = sender["email"]
    existing = find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        updated = update_contact(contact_id, sender, existing)
        changed = updated != existing
        add_note_to_contact(contact_id, sender)
        status = "Aggiornato" if changed else "Ignorato (già aggiornato)"
        return {"status": status, "email": email, "contact_id": contact_id}
    else:
        created = create_contact(sender)
        contact_id = created["id"]
        add_note_to_contact(contact_id, sender)
        return {"status": "Creato", "email": email, "contact_id": contact_id}


def sync_cycle(service, state: dict, dry_run: bool = False) -> list[dict]:
    """Run one sync cycle: fetch new messages and process each sender."""
    messages = get_new_messages(service, state)
    if not messages:
        log.info("No new messages.")
        return []

    processed_ids: set = set(state.get("processed_message_ids", []))
    results = []

    for msg in messages:
        msg_id = msg["id"]
        if msg_id in processed_ids:
            continue

        sender = get_message_sender(service, msg_id)
        if not sender:
            processed_ids.add(msg_id)
            continue

        log.info("Processing email from: %s <%s>", sender["display_name"], sender["email"])

        if dry_run:
            result = {"status": "DRY RUN", "email": sender["email"], "contact_id": "—"}
        else:
            try:
                result = process_sender(sender)
            except httpx.HTTPStatusError as exc:
                log.error("HubSpot error for %s: %s", sender["email"], exc.response.text)
                result = {"status": "Errore", "email": sender["email"], "contact_id": "—"}

        results.append(result)
        processed_ids.add(msg_id)

        log.info(
            "  → %-30s  %s  [ID: %s]",
            result["email"],
            result["status"],
            result["contact_id"],
        )

    # Keep only the last 5000 processed IDs to bound state file size
    state["processed_message_ids"] = list(processed_ids)[-5000:]
    save_state(state)
    return results


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to HubSpot")
    parser.add_argument(
        "--interval", type=int, default=POLL_INTERVAL_SECONDS,
        help=f"Polling interval in seconds (default: {POLL_INTERVAL_SECONDS})"
    )
    args = parser.parse_args()

    if not args.dry_run and not HUBSPOT_API_KEY:
        raise SystemExit("ERROR: Set the HUBSPOT_API_KEY environment variable.")

    log.info("Starting Gmail → HubSpot sync (interval=%ds, dry_run=%s)", args.interval, args.dry_run)
    service = get_gmail_service()
    state = load_state()

    while True:
        log.info("── Sync cycle at %s ──", datetime.now().strftime("%H:%M:%S"))
        results = sync_cycle(service, state, dry_run=args.dry_run)

        if results:
            print("\n┌─────────────────────────────────────────────────────────────┐")
            print(f"│ {'Email':<35} {'Stato':<20} {'ID HubSpot':<10} │")
            print("├─────────────────────────────────────────────────────────────┤")
            for r in results:
                print(f"│ {r['email']:<35} {r['status']:<20} {r['contact_id']:<10} │")
            print("└─────────────────────────────────────────────────────────────┘\n")

        if args.once:
            break

        log.info("Waiting %d seconds before next cycle…", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
