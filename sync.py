#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.

Output per email processed:
  Stato: Creato | Aggiornato | Ignorato
  Email contatto
  ID contatto HubSpot
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from email.utils import parseaddr
from typing import Optional

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate
from hubspot.crm.contacts.models import (
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))

# Addresses that should never be synced to CRM
IGNORED_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "notifications",
    "alerts", "support", "info", "hello", "team",
)
IGNORED_DOMAINS = {"gmail.com", "googlemail.com"}  # own mailbox domains to skip

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"history_id": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    creds: Optional[Credentials] = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, state: dict) -> tuple[list[dict], str]:
    """
    Returns (messages, new_history_id).
    On first run fetches the last 24h of INBOX messages.
    On subsequent runs uses historyId for efficient delta fetch.
    """
    history_id = state.get("history_id")

    if not history_id:
        # First run: pull recent INBOX messages
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=50, q="newer_than:1d")
            .execute()
        )
        messages = result.get("messages", [])
        # Grab current historyId from profile for future incremental fetches
        profile = service.users().getProfile(userId="me").execute()
        new_history_id = profile.get("historyId")
        return messages, new_history_id

    # Incremental fetch via History API
    try:
        history_result = (
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
    except HttpError as exc:
        if exc.resp.status == 404:
            # historyId expired; reset and retry from scratch next cycle
            log.warning("History ID expired, resetting state.")
            state["history_id"] = None
            save_state(state)
            return [], history_id
        raise

    new_history_id = history_result.get("historyId", history_id)
    messages = []
    for record in history_result.get("history", []):
        for added in record.get("messagesAdded", []):
            messages.append(added["message"])
    return messages, new_history_id


def get_message_headers(service, message_id: str) -> dict:
    """Fetch From/Subject headers for a message."""
    msg = (
        service.users()
        .messages()
        .get(userId="me", id=message_id, format="metadata",
             metadataHeaders=["From", "Subject"])
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Returns (email, full_name, domain).
    full_name may be empty string if not present in From header.
    """
    name, email = parseaddr(from_header)
    email = email.lower().strip()
    domain = email.split("@")[-1] if "@" in email else ""
    return email, name.strip(), domain


def should_ignore_sender(email: str, domain: str) -> bool:
    local = email.split("@")[0]
    if any(local.startswith(p) for p in IGNORED_PREFIXES):
        return True
    if domain in IGNORED_DOMAINS:
        return True
    if not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return True
    return False


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def get_hubspot_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(hs: hubspot.Client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    search_request = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(
                        property_name="email",
                        operator="EQ",
                        value=email,
                    )
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "lead_source"],
        limit=1,
    )
    try:
        response = hs.crm.contacts.search_api.do_search(
            public_object_search_request=search_request
        )
        if response.total > 0:
            return response.results[0]
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
    return None


def company_from_domain(domain: str) -> str:
    """Derive a company name guess from email domain (best-effort)."""
    # Strip common TLDs and capitalise
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]  # e.g. "acme" from "acme.com"
        return name.capitalize()
    return domain.capitalize()


def split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' into (firstname, lastname). Handles edge cases."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def create_contact(hs: hubspot.Client, email: str, name: str, domain: str) -> Optional[str]:
    """Create a new HubSpot contact and return its ID."""
    firstname, lastname = split_name(name)
    company = company_from_domain(domain) if domain else ""

    properties = {
        "email": email,
        "lead_source": "Gmail",
    }
    if firstname:
        properties["firstname"] = firstname
    if lastname:
        properties["lastname"] = lastname
    if company:
        properties["company"] = company

    try:
        contact = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=properties
            )
        )
        return contact.id
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", email, exc)
        return None


def update_contact(
    hs: hubspot.Client,
    contact_id: str,
    existing: dict,
    name: str,
    domain: str,
) -> bool:
    """
    Fill in any missing fields on an existing contact.
    Returns True if any update was made.
    """
    existing_props = existing.properties
    updates: dict[str, str] = {}

    firstname, lastname = split_name(name)
    company = company_from_domain(domain) if domain else ""

    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company
    if not existing_props.get("lead_source"):
        updates["lead_source"] = "Gmail"

    if not updates:
        return False

    try:
        from hubspot.crm.contacts import SimplePublicObjectInput
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error for contact %s: %s", contact_id, exc)
        return False


# ── Core sync loop ────────────────────────────────────────────────────────────

def process_message(
    service,
    hs: hubspot.Client,
    message_id: str,
    processed_ids: set,
) -> None:
    if message_id in processed_ids:
        return

    try:
        headers = get_message_headers(service, message_id)
    except HttpError as exc:
        log.warning("Could not fetch message %s: %s", message_id, exc)
        return

    from_header = headers.get("From", "")
    if not from_header:
        return

    email, name, domain = parse_sender(from_header)

    if should_ignore_sender(email, domain):
        log.info("Ignorato  | %s (sistema/noreply)", email)
        processed_ids.add(message_id)
        return

    existing = find_contact_by_email(hs, email)

    if existing is None:
        contact_id = create_contact(hs, email, name, domain)
        if contact_id:
            print(
                f"Stato: Creato  | Email: {email} | ID HubSpot: {contact_id}"
            )
        else:
            print(f"Stato: Errore  | Email: {email} | creazione fallita")
    else:
        contact_id = existing.id
        updated = update_contact(hs, contact_id, existing, name, domain)
        if updated:
            print(
                f"Stato: Aggiornato | Email: {email} | ID HubSpot: {contact_id}"
            )
        else:
            print(
                f"Stato: Ignorato   | Email: {email} | ID HubSpot: {contact_id} (già aggiornato)"
            )

    processed_ids.add(message_id)


def run() -> None:
    if not HUBSPOT_TOKEN:
        raise SystemExit("HUBSPOT_ACCESS_TOKEN non impostato nel file .env")

    log.info("Avvio sync Gmail → HubSpot (polling ogni %ds)", POLL_INTERVAL)
    service = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    while True:
        try:
            messages, new_history_id = fetch_new_messages(service, state)
            processed_ids: set = set(state.get("processed_message_ids", []))

            if messages:
                log.info("Trovati %d messaggi da processare", len(messages))
                for msg in messages:
                    process_message(service, hs, msg["id"], processed_ids)

            # Keep processed_ids list bounded (last 10 000 message IDs)
            state["history_id"] = new_history_id
            state["processed_message_ids"] = list(processed_ids)[-10_000:]
            save_state(state)

        except KeyboardInterrupt:
            log.info("Interruzione manuale — salvataggio stato e uscita.")
            save_state(state)
            break
        except Exception as exc:
            log.exception("Errore nel ciclo di sync: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
