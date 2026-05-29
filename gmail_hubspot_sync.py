#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot CRM.

Usage:
    python gmail_hubspot_sync.py          # continuous polling
    python gmail_hubspot_sync.py --once   # single run then exit
"""

import argparse
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException as ContactApiException

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]

SYNCED_LABEL_NAME = "HubSpot Synced"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
TOKEN_PATH = Path("token.json")
CREDENTIALS_PATH = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json"))

# Domains considered personal/generic — no company name extracted
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com",
    "proton.me", "mail.com", "ymail.com", "libero.it",
    "alice.it", "tiscali.it", "tin.it", "virgilio.it",
}


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def build_gmail_service():
    """Authenticate via OAuth2 and return Gmail API service."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {CREDENTIALS_PATH}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_or_create_label(service, name: str) -> str:
    """Return the ID of a Gmail label, creating it if absent."""
    labels = service.users().labels().list(userId="me").execute()
    for label in labels.get("labels", []):
        if label["name"] == name:
            return label["id"]

    result = service.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    log.info("Created Gmail label '%s' (id=%s)", name, result["id"])
    return result["id"]


def fetch_unsynced_messages(service, max_results: int = 50) -> list[dict]:
    """Return inbox messages that have not been labelled as synced yet."""
    # Gmail search: spaces in label names become hyphens
    label_slug = SYNCED_LABEL_NAME.lower().replace(" ", "-")
    query = f"in:inbox -label:{label_slug}"
    result = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
    )
    return result.get("messages", [])


def get_message_sender(service, msg_id: str) -> tuple[str, str, str]:
    """Return (from_header, subject, date) for a message."""
    msg = service.users().messages().get(
        userId="me",
        messageId=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return (
        headers.get("From", ""),
        headers.get("Subject", "(no subject)"),
        headers.get("Date", ""),
    )


def mark_as_synced(service, msg_id: str, label_id: str) -> None:
    """Apply the 'HubSpot Synced' label to a message."""
    service.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ── Contact parsing ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict | None:
    """
    Parse 'Display Name <email@domain.com>' or bare 'email@domain.com'.
    Returns a dict with keys: email, first_name, last_name, company, domain.
    Returns None if no valid email can be extracted.
    """
    name, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    # Split display name into first / last
    first_name, last_name = "", ""
    name = name.strip().strip('"')
    if name:
        parts = name.split(" ", 1)
        first_name = parts[0].strip()
        last_name = parts[1].strip() if len(parts) > 1 else ""

    domain = email.split("@", 1)[1]

    # Derive a company name from the domain when it is not a generic provider
    company = ""
    if domain not in GENERIC_DOMAINS:
        root = domain.split(".")[0]
        company = root.replace("-", " ").replace("_", " ").title()

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def build_hubspot_client() -> hubspot.Client:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is not set.")
    return hubspot.Client.create(access_token=token)


def find_contact(hs: hubspot.Client, email: str):
    """Search HubSpot for a contact by email. Returns the contact object or None."""
    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company", "lead_source"],
                "limit": 1,
            }
        )
        return resp.results[0] if resp.total > 0 else None
    except ContactApiException as exc:
        log.error("HubSpot search error for %s: %s", email, exc)
        return None


def create_contact(hs: hubspot.Client, sender: dict) -> str | None:
    """Create a new HubSpot contact. Returns the new contact ID or None on failure."""
    props = {
        "email": sender["email"],
        "lead_source": "OTHER",          # closest standard value; note body carries "Gmail"
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        log.info("Created contact %s → HubSpot ID %s", sender["email"], result.id)
        return result.id
    except ContactApiException as exc:
        log.error("HubSpot create error for %s: %s", sender["email"], exc)
        return None


def update_contact(hs: hubspot.Client, contact_id: str, sender: dict, existing) -> bool:
    """Fill in any blank fields on an existing contact. Returns True if updated."""
    existing_props = existing.properties or {}
    updates: dict[str, str] = {}

    if not existing_props.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not existing_props.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not existing_props.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if not updates:
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        log.info("Updated contact %s: filled %s", contact_id, list(updates.keys()))
        return True
    except ContactApiException as exc:
        log.error("HubSpot update error for %s: %s", contact_id, exc)
        return False


def add_timeline_note(hs: hubspot.Client, contact_id: str, subject: str, date_str: str) -> None:
    """Add an email-received engagement note to the contact's timeline."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"📥 Email ricevuta via Gmail\n"
        f"Oggetto: {subject}\n"
        f"Data: {date_str}\n"
        f"Fonte: Inbound Gmail"
    )
    try:
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": timestamp_ms,
                },
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 202,  # note → contact
                            }
                        ],
                    }
                ],
            }
        )
    except Exception as exc:
        log.warning("Could not add timeline note for contact %s: %s", contact_id, exc)


# ── Core processing ───────────────────────────────────────────────────────────

def process_message(
    gmail_svc,
    hs: hubspot.Client,
    msg_id: str,
    synced_label_id: str,
) -> dict:
    """
    Process a single Gmail message:
    1. Parse sender.
    2. Create or update HubSpot contact.
    3. Add timeline note.
    4. Label message as synced.

    Returns a result dict: {status, email, contact_id}.
    """
    from_header, subject, date_str = get_message_sender(gmail_svc, msg_id)
    sender = parse_sender(from_header)

    if sender is None:
        mark_as_synced(gmail_svc, msg_id, synced_label_id)
        return {"status": "Ignorato", "email": from_header or "(unknown)", "contact_id": None}

    email = sender["email"]
    existing = find_contact(hs, email)

    if existing:
        contact_id = existing.id
        changed = update_contact(hs, contact_id, sender, existing)
        status = "Aggiornato" if changed else "Ignorato"
    else:
        contact_id = create_contact(hs, sender)
        status = "Creato" if contact_id else "Errore"

    if contact_id:
        add_timeline_note(hs, contact_id, subject, date_str)

    mark_as_synced(gmail_svc, msg_id, synced_label_id)

    log.info("[%s] %-40s → ID HubSpot: %s", status, email, contact_id or "—")
    return {"status": status, "email": email, "contact_id": contact_id}


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(once: bool = False) -> None:
    log.info("Avvio Gmail → HubSpot sync (poll ogni %ds)", POLL_INTERVAL)

    gmail_svc = build_gmail_service()
    hs = build_hubspot_client()
    synced_label_id = get_or_create_label(gmail_svc, SYNCED_LABEL_NAME)

    while True:
        try:
            messages = fetch_unsynced_messages(gmail_svc)
            if messages:
                log.info("Trovate %d email da processare", len(messages))
                for msg in messages:
                    result = process_message(gmail_svc, hs, msg["id"], synced_label_id)
                    _print_result(result)
            else:
                log.debug("Nessuna nuova email.")
        except HttpError as exc:
            log.error("Gmail API error: %s", exc)
        except Exception as exc:
            log.error("Errore imprevisto: %s", exc, exc_info=True)

        if once:
            break
        time.sleep(POLL_INTERVAL)


def _print_result(r: dict) -> None:
    print(
        f"  Stato: {r['status']:<10}  "
        f"Email: {r['email']:<40}  "
        f"ID HubSpot: {r['contact_id'] or '—'}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending emails once and exit (no continuous loop)",
    )
    args = parser.parse_args()
    run(once=args.once)
