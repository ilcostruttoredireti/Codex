#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors the Gmail inbox and syncs senders as HubSpot contacts.

Usage:
    python gmail_hubspot_sync.py

First run will open a browser to complete Gmail OAuth2 authorization.
Set required env vars (see .env.example) before running.
"""

import email.utils
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    ApiException as HubSpotApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from hubspot.crm.objects.notes import (
    SimplePublicObjectInputForCreate as NoteInputForCreate,
)

load_dotenv()

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── config ────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))

HUBSPOT_ACCESS_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_IDS_FILE = Path(os.getenv("PROCESSED_IDS_FILE", "processed_ids.json"))

# Senders to skip (bots, notifications, no-reply addresses)
_IGNORED_PREFIXES = (
    "noreply@", "no-reply@", "donotreply@",
    "notifications@", "bounce@", "mailer@",
    "postmaster@", "daemon@", "newsletter@",
    "support@noreply", "info@noreply",
)
_IGNORED_DOMAINS = {
    "noreply.github.com",
    "notifications.github.com",
    "mailer.stripe.com",
    "mail.notion.so",
    "bounce.mail.google.com",
}


# ── Gmail helpers ─────────────────────────────────────────────────────────────
def get_gmail_service():
    creds = None
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


def fetch_inbox_messages(service, max_results: int = 50) -> list[dict]:
    """Return lightweight message stubs (id, threadId) from INBOX."""
    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
            .execute()
        )
        return resp.get("messages", [])
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return []


def get_message_headers(service, msg_id: str) -> dict[str, str]:
    """Fetch only the metadata headers we care about."""
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
    except HttpError as exc:
        log.warning("Gmail get message %s error: %s", msg_id, exc)
        return {}
    return {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }


def parse_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) normalised to lowercase."""
    name, addr = email.utils.parseaddr(from_header)
    return name.strip(), addr.strip().lower()


def extract_domain(addr: str) -> str:
    parts = addr.split("@", 1)
    return parts[1] if len(parts) == 2 else ""


def guess_company_from_domain(domain: str) -> str:
    """Strip TLD and numeric sub-parts, capitalise the main label."""
    if not domain:
        return ""
    labels = domain.split(".")
    # Drop last TLD label(s); take the meaningful part
    if len(labels) >= 2:
        main = labels[-2]
    else:
        main = labels[0]
    # Remove digits, dashes → title-case
    clean = re.sub(r"[-_0-9]+", " ", main).strip().title()
    return clean


def split_name(display_name: str) -> tuple[str, str]:
    """Naive first / last name split."""
    parts = display_name.split(" ", 1)
    first = parts[0] if parts else ""
    last = parts[1].strip() if len(parts) > 1 else ""
    return first, last


def is_bot_sender(addr: str, domain: str) -> bool:
    return (
        addr.startswith(_IGNORED_PREFIXES)
        or domain in _IGNORED_DOMAINS
        or not addr
    )


# ── HubSpot helpers ───────────────────────────────────────────────────────────
def get_hs_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client: hubspot.Client, addr: str):
    """Return the first matching HubSpot contact or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=addr)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
    except HubSpotApiException as exc:
        log.error("HubSpot search error for %s: %s", addr, exc)
        return None
    return resp.results[0] if resp.total > 0 else None


def _build_props(
    addr: str,
    first: str,
    last: str,
    company: str,
    only_missing: dict | None = None,
) -> dict[str, str]:
    """Build a property dict. If only_missing is given, skip already-set keys."""
    candidates = {
        "firstname": first,
        "lastname": last,
        "company": company,
        "leadsource": "Gmail",
    }
    if only_missing is not None:
        return {
            k: v
            for k, v in candidates.items()
            if v and not only_missing.get(k)
        }
    return {k: v for k, v in candidates.items() if v}


def create_contact(client: hubspot.Client, addr: str, first: str, last: str, company: str) -> str:
    props = {"email": addr, **_build_props(addr, first, last, company)}
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except HubSpotApiException as exc:
        log.error("HubSpot create contact %s error: %s", addr, exc)
        raise


def update_contact(
    client: hubspot.Client,
    contact_id: str,
    first: str,
    last: str,
    company: str,
    existing_props: dict,
) -> bool:
    """Update only fields that are currently empty. Returns True if any update was made."""
    updates = _build_props("", first, last, company, only_missing=existing_props)
    if not updates:
        return False
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
    except HubSpotApiException as exc:
        log.error("HubSpot update contact %s error: %s", contact_id, exc)
        return False
    return True


def add_gmail_note(client: hubspot.Client, contact_id: str, sender_email: str, subject: str):
    """Create a Note on the contact timeline recording the inbound email."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"[Inbound Gmail]\n"
        f"Email ricevuta da: {sender_email}\n"
        f"Oggetto: {subject}\n"
        f"Tag: Inbound Gmail"
    )
    note_input = NoteInputForCreate(
        properties={
            "hs_note_body": body,
            "hs_timestamp": str(now_ms),
        }
    )
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_input
        )
        # Associate note → contact (HubSpot built-in type 202)
        client.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_spec=[
                {
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 202,
                }
            ],
        )
    except HubSpotApiException as exc:
        log.warning("Could not create note for contact %s: %s", contact_id, exc)


# ── processed-ID store ────────────────────────────────────────────────────────
def load_processed_ids() -> set[str]:
    if PROCESSED_IDS_FILE.exists():
        return set(json.loads(PROCESSED_IDS_FILE.read_text()))
    return set()


def save_processed_ids(ids: set[str]):
    PROCESSED_IDS_FILE.write_text(json.dumps(sorted(ids), indent=2))


# ── per-message logic ─────────────────────────────────────────────────────────
def process_message(
    gmail_svc,
    hs_client: hubspot.Client,
    msg_id: str,
    processed_ids: set[str],
) -> dict | None:
    """
    Process one Gmail message. Returns a result dict or None if skipped.
    """
    if msg_id in processed_ids:
        return None

    headers = get_message_headers(gmail_svc, msg_id)
    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(nessun oggetto)")

    if not from_header:
        processed_ids.add(msg_id)
        return None

    display_name, sender_email = parse_sender(from_header)
    domain = extract_domain(sender_email)

    if is_bot_sender(sender_email, domain):
        processed_ids.add(msg_id)
        log.debug("Skipped bot sender: %s", sender_email)
        return None

    first, last = split_name(display_name)
    company = guess_company_from_domain(domain)

    existing = find_contact_by_email(hs_client, sender_email)

    try:
        if existing:
            updated = update_contact(
                hs_client,
                existing.id,
                first,
                last,
                company,
                existing.properties or {},
            )
            add_gmail_note(hs_client, existing.id, sender_email, subject)
            status = "Aggiornato" if updated else "Ignorato"
            contact_id = existing.id
        else:
            contact_id = create_contact(hs_client, sender_email, first, last, company)
            add_gmail_note(hs_client, contact_id, sender_email, subject)
            status = "Creato"
    except HubSpotApiException:
        processed_ids.add(msg_id)
        return None

    processed_ids.add(msg_id)
    return {"status": status, "email": sender_email, "hubspot_id": contact_id}


# ── main loop ─────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info("Gmail → HubSpot Sync avviato")
    log.info("Polling ogni %d secondi", POLL_INTERVAL)
    log.info("=" * 60)

    gmail_svc = get_gmail_service()
    hs_client = get_hs_client()
    processed_ids = load_processed_ids()
    log.info("ID già processati in cache: %d", len(processed_ids))

    try:
        while True:
            log.info("--- Polling Gmail inbox ---")
            messages = fetch_inbox_messages(gmail_svc)
            results = []

            for msg in messages:
                result = process_message(gmail_svc, hs_client, msg["id"], processed_ids)
                if result:
                    results.append(result)

            if results:
                log.info("%-12s | %-40s | %s", "Stato", "Email contatto", "ID HubSpot")
                log.info("-" * 70)
                for r in results:
                    log.info("%-12s | %-40s | %s", r["status"], r["email"], r["hubspot_id"])
            else:
                log.info("Nessun nuovo contatto da processare.")

            save_processed_ids(processed_ids)
            log.info("Prossimo check tra %d secondi…\n", POLL_INTERVAL)
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        log.info("Sync interrotto dall'utente.")
        save_processed_ids(processed_ids)


if __name__ == "__main__":
    run()
