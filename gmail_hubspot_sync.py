#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot CRM.
Avoids duplicates using email as unique key; updates existing contacts.
"""

import json
import os
import re
import time
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path("state.json")
TOKEN_FILE = Path("token.json")
CREDENTIALS_FILE = Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Domains to ignore when extracting company names
SKIP_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "me.com", "protonmail.com",
    "aol.com", "mail.com",
}


@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    source: str = "Gmail"


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) from a From: header."""
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>', from_header.strip())
    if match:
        full_name = match.group(1).strip()
        email = match.group(2).strip().lower()
    else:
        email = from_header.strip().lower()
        full_name = ""

    parts = full_name.split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return email, first, last


def domain_to_company(email: str) -> str:
    """Derive a company name from an email domain when it's a business address."""
    try:
        domain = email.split("@")[1].lower()
        if domain in SKIP_DOMAINS:
            return ""
        company = domain.split(".")[0].capitalize()
        return company
    except IndexError:
        return ""


def fetch_new_messages(service, processed_ids: set[str]) -> list[SenderContact]:
    """Fetch inbox messages not yet processed and return SenderContact list."""
    contacts: list[SenderContact] = []
    result = service.users().messages().list(
        userId="me", labelIds=["INBOX"], maxResults=50
    ).execute()
    messages = result.get("messages", [])

    for msg_stub in messages:
        msg_id = msg_stub["id"]
        if msg_id in processed_ids:
            continue

        msg = service.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["From", "Date"]
        ).execute()

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        from_header = headers.get("From", "")
        if not from_header:
            processed_ids.add(msg_id)
            continue

        email, first, last = parse_sender(from_header)
        if not email or "@" not in email:
            processed_ids.add(msg_id)
            continue

        company = domain_to_company(email)
        contacts.append(SenderContact(
            email=email,
            first_name=first,
            last_name=last,
            company=company,
        ))
        processed_ids.add(msg_id)
        log.debug("Parsed sender: %s (%s %s, %s)", email, first, last, company)

    return contacts


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

HUBSPOT_BASE = "https://api.hubapi.com"
HS_HEADERS = lambda: {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}


def hs_find_contact(email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    r = httpx.post(url, headers=HS_HEADERS(), json=payload, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(contact: SenderContact) -> str:
    """Create a new HubSpot contact and return its ID."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props = {
        "email": contact.email,
        "hs_lead_source": "Gmail",
    }
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company

    r = httpx.post(url, headers=HS_HEADERS(), json={"properties": props}, timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def hs_update_contact(contact_id: str, contact: SenderContact, existing: dict) -> None:
    """Patch only the missing fields on an existing HubSpot contact."""
    existing_props = existing.get("properties", {})
    updates: dict[str, str] = {}

    if contact.first_name and not existing_props.get("firstname"):
        updates["firstname"] = contact.first_name
    if contact.last_name and not existing_props.get("lastname"):
        updates["lastname"] = contact.last_name
    if contact.company and not existing_props.get("company"):
        updates["company"] = contact.company

    if not updates:
        return

    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = httpx.patch(url, headers=HS_HEADERS(), json={"properties": updates}, timeout=15)
    r.raise_for_status()


def hs_create_activity(contact_id: str, email: str) -> None:
    """Create a timeline note on the contact recording the inbound email."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    props = {
        "hs_note_body": f"📧 Inbound Gmail email received from {email}",
        "hs_timestamp": str(now_ms),
    }
    payload = {
        "properties": props,
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }],
    }
    r = httpx.post(url, headers=HS_HEADERS(), json=payload, timeout=15)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def sync_contact(contact: SenderContact, add_activity: bool = True) -> SyncResult:
    existing = hs_find_contact(contact.email)

    if existing:
        contact_id = existing["id"]
        hs_update_contact(contact_id, contact, existing)
        if add_activity:
            try:
                hs_create_activity(contact_id, contact.email)
            except Exception as e:
                log.warning("Could not create activity for %s: %s", contact.email, e)
        return SyncResult(status="Aggiornato", email=contact.email, hubspot_id=contact_id)

    contact_id = hs_create_contact(contact)
    if add_activity:
        try:
            hs_create_activity(contact_id, contact.email)
        except Exception as e:
            log.warning("Could not create activity for %s: %s", contact.email, e)
    return SyncResult(status="Creato", email=contact.email, hubspot_id=contact_id)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def load_state() -> set[str]:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()).get("processed_ids", []))
    return set()


def save_state(processed_ids: set[str]) -> None:
    STATE_FILE.write_text(json.dumps({"processed_ids": list(processed_ids)}, indent=2))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once(service, processed_ids: set[str]) -> list[SyncResult]:
    new_contacts = fetch_new_messages(service, processed_ids)

    # Deduplicate by email within this batch
    seen: dict[str, SenderContact] = {}
    for c in new_contacts:
        if c.email not in seen:
            seen[c.email] = c

    results: list[SyncResult] = []
    for contact in seen.values():
        try:
            result = sync_contact(contact)
            results.append(result)
            log.info("%-10s | %s | HubSpot ID: %s", result.status, result.email, result.hubspot_id)
        except Exception as e:
            log.error("Error syncing %s: %s", contact.email, e)
            results.append(SyncResult(status="Ignorato", email=contact.email))

    return results


def main():
    if not HUBSPOT_TOKEN:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN not set in .env")

    service = get_gmail_service()
    processed_ids = load_state()

    log.info("Gmail → HubSpot sync started. Poll interval: %ds", POLL_INTERVAL)

    while True:
        try:
            results = run_once(service, processed_ids)
            save_state(processed_ids)
            if results:
                log.info("Batch done: %d contacts processed", len(results))
        except Exception as e:
            log.error("Unexpected error: %s", e)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
