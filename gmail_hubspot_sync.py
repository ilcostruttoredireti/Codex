#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox, extracts sender info and creates/updates HubSpot contacts.
"""

import os
import re
import json
import time
import logging
import base64
import argparse
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

# Google
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# HubSpot
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

# ─── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "gmail_token.json"
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")

STATE_FILE = "sync_state.json"
LOG_FILE = "sync.log"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

CONTACT_SOURCE_LABEL = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domains to skip (internal, disposable, etc.)
IGNORED_DOMAINS = {
    "gmail.com", "googlemail.com", "noreply.com", "no-reply.com",
    "mailer-daemon", "bounce", "notifications", "donotreply",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


# ─── State persistence ────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_message_ids": [], "last_history_id": None}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Gmail auth ───────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


# ─── HubSpot client ───────────────────────────────────────────────────────────

def get_hubspot_client():
    if not HUBSPOT_API_KEY:
        raise ValueError("HUBSPOT_API_KEY environment variable is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


# ─── Email parsing ────────────────────────────────────────────────────────────

def extract_sender_info(message: dict) -> dict | None:
    """Extract email, first name, last name, and company from a Gmail message."""
    headers = {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    raw_from = headers.get("from", "")
    if not raw_from:
        return None

    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()

    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]

    # Skip no-reply / internal senders
    if any(skip in domain or skip in email_addr for skip in IGNORED_DOMAINS):
        log.debug("Skipping sender %s (ignored domain/pattern)", email_addr)
        return None

    # Parse first / last name from display name
    first_name, last_name = "", ""
    clean_name = display_name.strip().strip('"').strip("'")
    if clean_name:
        parts = clean_name.split(maxsplit=1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    # Derive company name from domain (strip TLD, capitalise)
    company = _domain_to_company(domain)

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "message_id": message.get("id", ""),
    }


def _domain_to_company(domain: str) -> str:
    """Turn 'acmecorp.com' → 'Acmecorp', 'my-company.io' → 'My Company'."""
    # Remove common TLDs
    base = re.sub(r"\.(com|org|net|io|co|it|eu|de|fr|es|uk|biz|info)(\.\w+)?$", "", domain)
    # Replace hyphens/dots with spaces, title-case
    return base.replace("-", " ").replace(".", " ").title()


# ─── HubSpot operations ───────────────────────────────────────────────────────

def find_contact_by_email(hs: hubspot.Client, email: str) -> dict | None:
    """Return the HubSpot contact dict if found, else None."""
    filter_obj = Filter(property_name="email", operator="EQ", value=email)
    filter_group = FilterGroup(filters=[filter_obj])
    search_request = PublicObjectSearchRequest(
        filter_groups=[filter_group],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    try:
        response = hs.crm.contacts.search_api.do_search(
            public_object_search_request=search_request
        )
        if response.results:
            return response.results[0]
    except ApiException as e:
        log.error("HubSpot search error for %s: %s", email, e)
    return None


def create_contact(hs: hubspot.Client, sender: dict) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (status, contact_id)."""
    props = _build_properties(sender)
    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        contact_id = result.id
        log.info("CREATO   | %s | HubSpot ID: %s", sender["email"], contact_id)
        return "Creato", contact_id
    except ApiException as e:
        # 409 = contact already exists (race condition)
        if e.status == 409:
            log.warning("Conflitto creazione %s, riprovo come aggiornamento", sender["email"])
            existing = find_contact_by_email(hs, sender["email"])
            if existing:
                return update_contact(hs, existing.id, sender)
        log.error("Errore creazione contatto %s: %s", sender["email"], e)
        return "Errore", ""


def update_contact(hs: hubspot.Client, contact_id: str, sender: dict) -> tuple[str, str]:
    """Update only the missing fields of an existing HubSpot contact."""
    # Fetch current values first
    try:
        current = hs.crm.contacts.basic_api.get_by_id(
            contact_id,
            properties=["firstname", "lastname", "company", "hs_lead_status"],
        )
    except ApiException as e:
        log.error("Errore lettura contatto %s: %s", contact_id, e)
        return "Errore", contact_id

    current_props = current.properties or {}
    updates = {}

    # Only fill in blank fields
    field_map = {
        "firstname": sender["first_name"],
        "lastname": sender["last_name"],
        "company": sender["company"],
    }
    for field, new_val in field_map.items():
        if new_val and not current_props.get(field):
            updates[field] = new_val

    if not updates:
        log.info("IGNORATO | %s | ID: %s (nessun campo da aggiornare)", sender["email"], contact_id)
        return "Ignorato", contact_id

    try:
        from hubspot.crm.contacts import SimplePublicObjectInput
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        log.info("AGGIORNATO | %s | HubSpot ID: %s | Campi: %s", sender["email"], contact_id, list(updates.keys()))
        return "Aggiornato", contact_id
    except ApiException as e:
        log.error("Errore aggiornamento contatto %s: %s", contact_id, e)
        return "Errore", contact_id


def _build_properties(sender: dict) -> dict:
    props = {
        "email": sender["email"],
        "hs_lead_status": "NEW",
        "leadsource": CONTACT_SOURCE_LABEL,
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]
    return props


# ─── Activity timeline ────────────────────────────────────────────────────────

def log_email_activity(hs: hubspot.Client, contact_id: str, sender: dict) -> None:
    """Create a note on the contact's timeline recording the inbound email."""
    try:
        note_body = (
            f"📧 Email ricevuta via Gmail\n"
            f"Oggetto: {sender.get('subject', 'N/A')}\n"
            f"Data: {sender.get('date', 'N/A')}\n"
            f"Tag: {CONTACT_TAG}"
        )
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                    }
                ],
            )
        )
        log.debug("Timeline activity aggiunta per contatto %s", contact_id)
    except Exception as e:
        log.warning("Impossibile aggiungere activity per %s: %s", contact_id, e)


# ─── Core sync logic ──────────────────────────────────────────────────────────

def fetch_new_messages(gmail, last_message_ids: set, max_results: int = 50) -> list[dict]:
    """Fetch unread inbox messages not yet processed."""
    try:
        result = gmail.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            q="is:unread",
            maxResults=max_results,
        ).execute()
    except HttpError as e:
        log.error("Errore Gmail API: %s", e)
        return []

    messages = result.get("messages", [])
    new_messages = []

    for msg_stub in messages:
        msg_id = msg_stub["id"]
        if msg_id in last_message_ids:
            continue
        try:
            full_msg = gmail.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            new_messages.append(full_msg)
        except HttpError as e:
            log.error("Errore lettura messaggio %s: %s", msg_id, e)

    return new_messages


def process_message(hs: hubspot.Client, message: dict, add_activity: bool = True) -> dict:
    """Process a single Gmail message and sync the sender to HubSpot."""
    result = {"status": "Ignorato", "email": "", "hubspot_id": ""}

    sender = extract_sender_info(message)
    if not sender:
        return result

    result["email"] = sender["email"]

    existing = find_contact_by_email(hs, sender["email"])

    if existing:
        status, contact_id = update_contact(hs, existing.id, sender)
    else:
        status, contact_id = create_contact(hs, sender)

    result["status"] = status
    result["hubspot_id"] = contact_id

    if contact_id and add_activity and status in ("Creato", "Aggiornato"):
        log_email_activity(hs, contact_id, sender)

    return result


def run_sync_cycle(gmail, hs: hubspot.Client, state: dict, add_activity: bool = True) -> list[dict]:
    """Run one full sync cycle. Returns list of result dicts."""
    processed_ids = set(state.get("processed_message_ids", []))

    messages = fetch_new_messages(gmail, processed_ids)
    log.info("Trovati %d nuovi messaggi da processare", len(messages))

    results = []
    for message in messages:
        res = process_message(hs, message, add_activity=add_activity)
        results.append(res)
        # Mark as processed even if ignored
        processed_ids.add(message["id"])

    # Keep only the last 5000 IDs to avoid unbounded growth
    state["processed_message_ids"] = list(processed_ids)[-5000:]
    save_state(state)

    return results


def print_summary(results: list[dict]) -> None:
    if not results:
        print("\nNessun nuovo messaggio processato.\n")
        return
    print("\n" + "─" * 60)
    print(f"{'STATO':<12} {'EMAIL':<35} {'HUBSPOT ID'}")
    print("─" * 60)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<35} {r['hubspot_id']}")
    print("─" * 60)

    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"  Creati: {counts['Creato']}  |  Aggiornati: {counts['Aggiornato']}  |  Ignorati: {counts['Ignorato']}  |  Errori: {counts['Errore']}")
    print("─" * 60 + "\n")


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS, help="Poll interval in seconds")
    parser.add_argument("--no-activity", action="store_true", help="Skip HubSpot timeline activity logging")
    args = parser.parse_args()

    log.info("Avvio Gmail → HubSpot sync")
    gmail = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()
    add_activity = not args.no_activity

    if args.once:
        results = run_sync_cycle(gmail, hs, state, add_activity)
        print_summary(results)
        return

    log.info("Monitoraggio continuo ogni %d secondi. Premi Ctrl+C per uscire.", args.interval)
    while True:
        try:
            results = run_sync_cycle(gmail, hs, state, add_activity)
            print_summary(results)
        except KeyboardInterrupt:
            log.info("Sync interrotto dall'utente.")
            break
        except Exception as e:
            log.error("Errore inaspettato nel ciclo di sync: %s", e, exc_info=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
