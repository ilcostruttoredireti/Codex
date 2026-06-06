#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs new senders as HubSpot contacts.
"""

import os
import re
import json
import time
import email as email_lib
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from email.utils import parseaddr, getaddresses

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Domains to skip (system senders, noreply, etc.)
IGNORED_DOMAINS = {
    "legalmail.it", "pec.it", "pec.gov.it", "mailer-daemon",
    "noreply", "no-reply", "donotreply", "mailerdaemon",
    "amazonses.com", "sendgrid.net", "mailgun.org",
}
IGNORED_PREFIXES = ("noreply@", "no-reply@", "donotreply@", "mailer-daemon@", "bounce@", "bounce+")

# HubSpot contact source label
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")


# ---------------------------------------------------------------------------
# State management (persists processed message IDs)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_message_ids": [], "last_history_id": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_inbox_messages(service, max_results: int = 50) -> list[dict]:
    """Return recent INBOX messages (newest first)."""
    result = service.users().messages().list(
        userId="me",
        labelIds=["INBOX"],
        maxResults=max_results,
    ).execute()
    return result.get("messages", [])


def get_message_detail(service, msg_id: str) -> Optional[dict]:
    """Fetch full message headers."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Date", "Subject"],
        ).execute()
        return msg
    except HttpError as e:
        log.warning("Failed to fetch message %s: %s", msg_id, e)
        return None


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Parse 'From' header → (display_name, email, domain)."""
    name, addr = parseaddr(from_header)
    addr = addr.lower().strip()
    domain = addr.split("@")[-1] if "@" in addr else ""
    return name.strip(), addr, domain


def extract_name_parts(display_name: str) -> tuple[str, str]:
    """Split display name into (first_name, last_name)."""
    parts = display_name.split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def company_from_domain(domain: str) -> str:
    """Derive a rough company name from the email domain."""
    if not domain or domain in IGNORED_DOMAINS:
        return ""
    # Strip common TLDs to get a readable name
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return domain.title()


def should_skip(email_addr: str, domain: str) -> bool:
    if not email_addr or "@" not in email_addr:
        return True
    if any(email_addr.startswith(p) for p in IGNORED_PREFIXES):
        return True
    for ignored in IGNORED_DOMAINS:
        if domain == ignored or domain.endswith("." + ignored):
            return True
    return False


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(email_addr: str) -> Optional[dict]:
    """Search HubSpot for a contact with the given email."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email_addr}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(properties: dict) -> dict:
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=_hs_headers(), json={"properties": properties}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, properties: dict) -> dict:
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": properties}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def add_note_to_contact(contact_id: str, subject: str, body: str, timestamp_ms: int) -> None:
    """Create a NOTE engagement and associate it with the contact."""
    url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes"
    note_props = {
        "hs_note_body": body,
        "hs_timestamp": str(timestamp_ms),
    }
    payload = {
        "properties": note_props,
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=10)
    if not resp.ok:
        log.warning("Could not create note for contact %s: %s", contact_id, resp.text)


def build_contact_properties(
    email_addr: str,
    display_name: str,
    domain: str,
    existing: Optional[dict] = None,
) -> dict:
    """Build HubSpot property dict, filling only missing fields when updating."""
    first, last = extract_name_parts(display_name)
    company = company_from_domain(domain)

    if existing:
        ep = existing.get("properties", {})
        props = {}
        if not ep.get("firstname") and first:
            props["firstname"] = first
        if not ep.get("lastname") and last:
            props["lastname"] = last
        if not ep.get("company") and company:
            props["company"] = company
        # Always keep source tag updated
        props["lead_source"] = CONTACT_SOURCE
        props["hs_tag"] = INBOUND_TAG
        return props

    # New contact — fill everything available
    props: dict = {
        "email": email_addr,
        "lead_source": CONTACT_SOURCE,
        "hs_tag": INBOUND_TAG,
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company
    return props


# ---------------------------------------------------------------------------
# Result dataclass (simple dict)
# ---------------------------------------------------------------------------

def make_result(status: str, email_addr: str, contact_id: str) -> dict:
    return {"status": status, "email": email_addr, "hubspot_id": contact_id}


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_message(msg: dict, state: dict) -> Optional[dict]:
    """
    Process a single Gmail message.
    Returns a result dict or None if skipped.
    """
    msg_id = msg["id"]
    if msg_id in state["processed_message_ids"]:
        return None

    # Always mark as processed to avoid retrying failed messages
    state["processed_message_ids"].append(msg_id)
    # Keep state size bounded
    if len(state["processed_message_ids"]) > 10_000:
        state["processed_message_ids"] = state["processed_message_ids"][-5_000:]

    detail = msg  # already has headers if fetched with metadata
    headers = {h["name"]: h["value"] for h in detail.get("payload", {}).get("headers", [])}

    from_header = headers.get("From", "")
    if not from_header:
        return None

    display_name, email_addr, domain = parse_sender(from_header)

    if should_skip(email_addr, domain):
        log.debug("Skipping system sender: %s", email_addr)
        return None

    subject = headers.get("Subject", "(no subject)")
    date_str = headers.get("Date", "")
    try:
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    except Exception:
        ts_ms = int(time.time() * 1000)

    log.info("Processing email from %s <%s>", display_name, email_addr)

    existing = find_contact_by_email(email_addr)

    if existing:
        contact_id = existing["id"]
        props = build_contact_properties(email_addr, display_name, domain, existing=existing)
        if props:
            update_contact(contact_id, props)
            status = "Aggiornato"
        else:
            status = "Ignorato (nessun campo da aggiornare)"
    else:
        props = build_contact_properties(email_addr, display_name, domain)
        new_contact = create_contact(props)
        contact_id = new_contact["id"]
        status = "Creato"

    # Add timeline note
    note_body = (
        f"Email ricevuta da: {display_name} <{email_addr}>\n"
        f"Oggetto: {subject}\n"
        f"Data: {date_str}\n"
        f"Fonte: Gmail (sync automatico)"
    )
    add_note_to_contact(contact_id, f"Email: {subject}", note_body, ts_ms)

    result = make_result(status, email_addr, contact_id)
    log.info("  → %s | ID HubSpot: %s", status, contact_id)
    return result


def sync_once(service, state: dict) -> list[dict]:
    """Fetch inbox, process new messages, return results."""
    results = []
    messages = get_inbox_messages(service, max_results=50)

    for msg_stub in messages:
        msg_id = msg_stub["id"]
        if msg_id in state["processed_message_ids"]:
            continue
        detail = get_message_detail(service, msg_id)
        if not detail:
            continue
        result = process_message(detail, state)
        if result:
            results.append(result)

    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        log.info("Nessun nuovo contatto da processare.")
        return
    log.info("\n%-10s  %-35s  %s", "Stato", "Email", "ID HubSpot")
    log.info("-" * 70)
    for r in results:
        log.info("%-10s  %-35s  %s", r["status"], r["email"], r["hubspot_id"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    if not HUBSPOT_API_KEY:
        raise SystemExit("HUBSPOT_API_KEY non impostato. Leggi .env.example per la configurazione.")

    log.info("Avvio Gmail → HubSpot Sync (polling ogni %ds)", POLL_INTERVAL)
    state = load_state()

    try:
        service = get_gmail_service()
    except Exception as e:
        raise SystemExit(f"Impossibile autenticarsi con Gmail: {e}")

    while True:
        log.info("--- Controllo nuove email [%s] ---", datetime.now().strftime("%H:%M:%S"))
        try:
            results = sync_once(service, state)
            print_summary(results)
        except Exception as e:
            log.error("Errore durante la sincronizzazione: %s", e)
        finally:
            save_state(state)

        log.info("Prossimo controllo tra %d secondi...\n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
