#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts sender info, creates/updates HubSpot contacts.

Usage:
    python gmail_hubspot_sync.py          # continuous polling
    python gmail_hubspot_sync.py --once   # single run and exit
"""

import argparse
import email.utils
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
STATE_FILE = Path(".sync_state.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

HUBSPOT_BASE = "https://api.hubapi.com"

# Free email providers — domain is not used as company name
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "icloud.com", "me.com",
    "aol.com", "live.com", "live.it", "protonmail.com", "libero.it",
    "alice.it", "virgilio.it", "tiscali.it",
}

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO"), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State persistence (tracks last Gmail historyId + processed message IDs)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds: Optional[Credentials] = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Parse 'From' header → (email_addr, firstname, lastname)."""
    name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    firstname, lastname = "", ""
    if name:
        parts = name.strip().split(" ", 1)
        firstname = parts[0].capitalize()
        lastname = parts[1].strip().capitalize() if len(parts) > 1 else ""
    return addr, firstname, lastname


def company_from_domain(email_addr: str) -> str:
    """Derive a company name from the email domain, skipping free providers."""
    if "@" not in email_addr:
        return ""
    domain = email_addr.split("@")[-1].lower()
    if domain in FREE_DOMAINS:
        return ""
    # e.g. "acme.com" → "Acme", "mail.bigcorp.it" → "Bigcorp"
    parts = domain.split(".")
    meaningful = parts[-2] if len(parts) >= 2 else parts[0]
    return meaningful.capitalize()


def fetch_new_messages(service, state: dict) -> list[dict]:
    """
    Returns new INBOX messages since the last run.
    On first run, bootstraps historyId and returns the 20 most recent messages.
    """
    history_id = state.get("history_id")

    if not history_id:
        profile = service.users().getProfile(userId="me").execute()
        state["history_id"] = profile["historyId"]
        save_state(state)
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=20)
            .execute()
        )
        return result.get("messages", [])

    try:
        history = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
        new_msgs: list[dict] = []
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                new_msgs.append(added["message"])
        state["history_id"] = history.get("historyId", history_id)
        save_state(state)
        return new_msgs
    except HttpError as exc:
        if exc.resp.status == 404:
            log.warning("historyId scaduto — reset dello stato.")
            state.pop("history_id", None)
            save_state(state)
        raise


def get_sender_header(service, message_id: str) -> Optional[str]:
    """Return the 'From' header value for a given message ID."""
    try:
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
        for header in msg.get("payload", {}).get("headers", []):
            if header["name"].lower() == "from":
                return header["value"]
    except HttpError as exc:
        log.warning("Impossibile recuperare messaggio %s: %s", message_id, exc)
    return None


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def _hs_post(path: str, payload: dict) -> dict:
    r = requests.post(f"{HUBSPOT_BASE}{path}", headers=_hs_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _hs_patch(path: str, payload: dict) -> dict:
    r = requests.patch(f"{HUBSPOT_BASE}{path}", headers=_hs_headers(), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def _hs_put(path: str) -> None:
    requests.put(f"{HUBSPOT_BASE}{path}", headers=_hs_headers(), timeout=15)


def find_contact(email_addr: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the record or None."""
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email_addr,
            }]
        }],
        "properties": ["email", "firstname", "lastname", "company", "lifecyclestage"],
        "limit": 1,
    }
    data = _hs_post("/crm/v3/objects/contacts/search", payload)
    results = data.get("results", [])
    return results[0] if results else None


def create_contact(
    email_addr: str,
    firstname: str,
    lastname: str,
    company: str,
) -> dict:
    props = {
        "email": email_addr,
        "lifecyclestage": "lead",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return _hs_post("/crm/v3/objects/contacts", {"properties": props})


def update_contact_fields(contact_id: str, updates: dict) -> dict:
    return _hs_patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})


def add_inbound_email_note(contact_id: str, sender_email: str) -> str:
    """Create a note on the contact recording the inbound Gmail event."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    note = _hs_post(
        "/crm/v3/objects/notes",
        {
            "properties": {
                "hs_timestamp": str(now_ms),
                "hs_note_body": (
                    f"📧 Email inbound ricevuta da {sender_email}\n"
                    f"Fonte: Gmail  |  Tag: Inbound Gmail\n"
                    f"Sincronizzato il {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
                ),
            }
        },
    )
    note_id = note["id"]
    # Associate note ↔ contact
    _hs_put(
        f"/crm/v3/objects/notes/{note_id}"
        f"/associations/contacts/{contact_id}/note_to_contact"
    )
    return note_id


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def sync_contact(email_addr: str, firstname: str, lastname: str) -> dict:
    """
    Upsert a contact in HubSpot.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "id": ...}
    """
    if not email_addr or "@" not in email_addr:
        return {"status": "Ignorato", "email": email_addr, "id": None}

    company = company_from_domain(email_addr)
    existing = find_contact(email_addr)

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict = {}
        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company") and company:
            updates["company"] = company
        if updates:
            update_contact_fields(contact_id, updates)
        add_inbound_email_note(contact_id, email_addr)
        return {"status": "Aggiornato", "email": email_addr, "id": contact_id}

    contact = create_contact(email_addr, firstname, lastname, company)
    contact_id = contact["id"]
    add_inbound_email_note(contact_id, email_addr)
    return {"status": "Creato", "email": email_addr, "id": contact_id}


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run(once: bool = False) -> None:
    if not HUBSPOT_API_KEY:
        log.error("HUBSPOT_API_KEY non impostata. Aggiungila nel file .env o come variabile d'ambiente.")
        sys.exit(1)

    service = get_gmail_service()
    state = load_state()

    log.info("=== Gmail → HubSpot Sync avviato ===")
    if not once:
        log.info("Polling ogni %d secondi. Premi Ctrl+C per fermare.", POLL_INTERVAL)

    while True:
        try:
            messages = fetch_new_messages(service, state)
            seen: list = state.setdefault("seen_ids", [])
            new_count = 0

            for msg in messages:
                msg_id = msg["id"]
                if msg_id in seen:
                    continue

                from_header = get_sender_header(service, msg_id)
                if not from_header:
                    seen.append(msg_id)
                    continue

                email_addr, firstname, lastname = parse_sender(from_header)
                result = sync_contact(email_addr, firstname, lastname)

                seen.append(msg_id)
                save_state(state)
                new_count += 1

                status_icon = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–"}.get(
                    result["status"], "?"
                )
                log.info(
                    "%s %-12s  email=%-40s  hs_id=%s",
                    status_icon,
                    result["status"],
                    result["email"],
                    result["id"] or "—",
                )

            if new_count == 0 and messages:
                log.debug("Nessun messaggio nuovo da processare.")

            # Keep seen list bounded
            if len(seen) > 1000:
                state["seen_ids"] = seen[-500:]
                save_state(state)

        except KeyboardInterrupt:
            log.info("Interruzione manuale. Uscita.")
            break
        except Exception as exc:
            log.error("Errore durante il ciclo di sync: %s", exc, exc_info=True)

        if once:
            break

        time.sleep(POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un singolo ciclo ed esci (utile per cron job)",
    )
    args = parser.parse_args()
    run(once=args.once)
