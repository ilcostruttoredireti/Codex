#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=====================================================
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.

For each new email:
  - Extracts sender: email, name, company (inferred from domain)
  - Searches HubSpot for existing contact (keyed on email)
  - Creates a new contact or updates missing fields on an existing one
  - Adds an activity note tagged "Inbound Gmail"

Output per email:
  Status: Creato | Aggiornato | Ignorato
  Email contatto
  ID contatto HubSpot
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
GMAIL_CREDENTIALS_FILE: str = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE: str = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE: Path = Path(os.getenv("STATE_FILE", "sync_state.json"))
INBOX_MAX_RESULTS: int = int(os.getenv("INBOX_MAX_RESULTS", "50"))
TAG_LABEL: str = "Inbound Gmail"

# Domains considered personal — no company name inferred from these
PERSONAL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
        "outlook.com", "outlook.it", "hotmail.com", "hotmail.it",
        "icloud.com", "me.com", "mac.com", "live.com", "live.it",
        "protonmail.com", "pm.me", "aol.com",
        "libero.it", "alice.it", "tin.it", "virgilio.it", "tiscali.it",
        "fastwebnet.it", "email.it", "katamail.com", "inwind.it",
    }
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class SenderInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""


@dataclass
class SyncResult:
    status: str  # Creato | Aggiornato | Ignorato
    email: str
    contact_id: Optional[str]
    subject: str = ""


# ── State management ──────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail authentication ──────────────────────────────────────────────────────


def get_gmail_service():
    """Return an authenticated Gmail API service object."""
    creds: Optional[Credentials] = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ── Sender extraction ─────────────────────────────────────────────────────────


def _split_name(display_name: str) -> tuple:
    """Return (firstname, lastname) from a display name string."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Infer a company name from a domain, empty for personal domains."""
    low = domain.lower()
    if low in PERSONAL_DOMAINS:
        return ""
    base = re.sub(r"\.[^.]+$", "", low)     # drop TLD
    base = re.sub(r"[\-.]", " ", base)      # dashes/dots → spaces
    return base.title()


def extract_sender(from_header: str) -> SenderInfo:
    """Parse a From: header into a SenderInfo."""
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()
    if not email_addr or "@" not in email_addr:
        return SenderInfo(email="")
    domain = email_addr.split("@")[1]
    firstname, lastname = _split_name(display_name)
    company = _company_from_domain(domain)
    return SenderInfo(
        email=email_addr,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
    )


# ── Gmail fetching ────────────────────────────────────────────────────────────


def list_new_inbox_messages(service, processed_ids: set) -> list:
    """Return message stubs from INBOX that haven't been processed yet."""
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=INBOX_MAX_RESULTS)
            .execute()
        )
        return [m for m in result.get("messages", []) if m["id"] not in processed_ids]
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return []


def fetch_message_headers(service, msg_id: str) -> dict:
    """Fetch From / Subject / Date headers for a single message."""
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        hdrs = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "id": msg_id,
            "from": hdrs.get("From", ""),
            "subject": hdrs.get("Subject", ""),
            "date": hdrs.get("Date", ""),
        }
    except HttpError as exc:
        log.error("Gmail get error (msg %s): %s", msg_id, exc)
        return {}


# ── HubSpot API ───────────────────────────────────────────────────────────────

HS_BASE = "https://api.hubapi.com"


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    r = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: SenderInfo) -> dict:
    """Create a new HubSpot contact from sender info."""
    props: dict = {"email": sender.email, "hs_lead_status": "NEW"}
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company

    r = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
    )
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, sender: SenderInfo, existing: dict) -> bool:
    """Patch only empty fields on an existing contact. Returns True if updated."""
    ep = existing.get("properties", {})
    updates: dict = {}
    if sender.firstname and not ep.get("firstname"):
        updates["firstname"] = sender.firstname
    if sender.lastname and not ep.get("lastname"):
        updates["lastname"] = sender.lastname
    if sender.company and not ep.get("company"):
        updates["company"] = sender.company

    if not updates:
        return False

    r = requests.patch(
        f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
    )
    r.raise_for_status()
    return True


def hs_add_note(contact_id: str, subject: str, sender_email: str) -> None:
    """Add an activity note associated with the contact."""
    ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    note_body = (
        f"<b>📧 Email ricevuta via Gmail</b><br>"
        f"Da: {sender_email}<br>"
        f"Oggetto: {subject or '(nessun oggetto)'}<br>"
        f"Tag: {TAG_LABEL}<br>"
        f"Data: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": ts_ms,
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,
                    }
                ],
            }
        ],
    }
    r = requests.post(
        f"{HS_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json=payload,
    )
    if not r.ok:
        log.warning("Note creation failed for contact %s: %s", contact_id, r.text)


# ── Sync logic ────────────────────────────────────────────────────────────────


def process_message(sender: SenderInfo, subject: str) -> SyncResult:
    """Upsert a HubSpot contact for the given sender and log an activity."""
    try:
        existing = hs_find_contact(sender.email)
        if existing:
            contact_id = existing["id"]
            hs_update_contact(contact_id, sender, existing)
            hs_add_note(contact_id, subject, sender.email)
            return SyncResult("Aggiornato", sender.email, contact_id, subject)
        else:
            new_contact = hs_create_contact(sender)
            contact_id = new_contact["id"]
            hs_add_note(contact_id, subject, sender.email)
            return SyncResult("Creato", sender.email, contact_id, subject)
    except requests.HTTPError as exc:
        log.error(
            "HubSpot API error for %s: %s",
            sender.email,
            exc.response.text if exc.response else exc,
        )
        return SyncResult("Ignorato", sender.email, None, subject)
    except Exception as exc:
        log.error("Unexpected error for %s: %s", sender.email, exc)
        return SyncResult("Ignorato", sender.email, None, subject)


def _print_result(result: SyncResult) -> None:
    icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}.get(result.status, "❓")
    log.info(
        "%s %-12s | Email: %-42s | HubSpot ID: %-12s | Oggetto: %s",
        icon,
        result.status,
        result.email,
        result.contact_id or "N/A",
        (result.subject or "")[:60],
    )


# ── Main loop ─────────────────────────────────────────────────────────────────


def run_once(gmail_service, processed_ids: set) -> list:
    """
    Process all new inbox messages once.
    Returns list of SyncResult and updates processed_ids in-place.
    """
    new_messages = list_new_inbox_messages(gmail_service, processed_ids)
    log.info("%d new message(s) found in inbox.", len(new_messages))

    results: list = []
    for stub in new_messages:
        msg_id = stub["id"]
        data = fetch_message_headers(gmail_service, msg_id)
        processed_ids.add(msg_id)  # mark regardless of outcome

        if not data or not data.get("from"):
            continue

        sender = extract_sender(data["from"])
        if not sender.email:
            continue

        result = process_message(sender, data.get("subject", ""))
        _print_result(result)
        results.append(result)

    return results


def main() -> None:
    if not HUBSPOT_TOKEN:
        raise RuntimeError(
            "HUBSPOT_ACCESS_TOKEN environment variable is not set. "
            "Copy .env.example to .env and fill in the values."
        )

    log.info("Starting Gmail → HubSpot sync (poll every %ds).", POLL_INTERVAL)
    state = load_state()
    processed_ids: set = set(state.get("processed_message_ids", []))

    gmail = get_gmail_service()

    while True:
        log.info("── Polling inbox ──────────────────────────────────────────────────")
        run_once(gmail, processed_ids)

        # Persist state; cap list to avoid unbounded growth
        state["processed_message_ids"] = list(processed_ids)[-20_000:]
        save_state(state)

        log.info("Sleeping %ds…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
