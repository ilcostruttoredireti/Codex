"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.
Avoids duplicates (email as unique key) and updates existing records.
"""

import os
import json
import time
import base64
import logging
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    ApiException as ContactApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ───────────────────────────────────────────────────────────────

load_dotenv()

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

SYSTEM_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load persisted state (last processed historyId + processed message IDs)."""
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail helpers ────────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found: {GMAIL_CREDENTIALS_FILE}\n"
                    "Download OAuth 2.0 credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_new_messages(service, history_id: Optional[str]) -> tuple[list[dict], str]:
    """
    Return (new_messages, latest_history_id).
    On first run (no history_id) returns the 50 most recent inbox messages.
    """
    profile = service.users().getProfile(userId="me").execute()
    latest_history_id = profile["historyId"]

    if not history_id:
        log.info("First run — fetching the 50 most recent inbox messages.")
        results = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=50)
            .execute()
        )
        return results.get("messages", []), latest_history_id

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
        messages = []
        for record in history.get("history", []):
            for added in record.get("messagesAdded", []):
                messages.append(added["message"])
        return messages, latest_history_id
    except HttpError as e:
        if e.resp.status == 404:
            # historyId expired — restart from scratch
            log.warning("History ID expired, restarting from recent messages.")
            return get_new_messages(service, None)
        raise


def parse_sender(raw_from: str) -> tuple[str, str, str]:
    """
    Parse a From header into (email, full_name, first_name, last_name).
    Returns (email, display_name, domain).
    """
    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.lower().strip()
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    return email_addr, display_name.strip(), domain


def get_message_sender(service, message_id: str) -> Optional[tuple[str, str, str]]:
    """Fetch a message and extract sender (email, name, domain)."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        if not raw_from:
            return None
        email_addr, display_name, domain = parse_sender(raw_from)
        if not email_addr or "@" not in email_addr:
            return None
        return email_addr, display_name, domain, headers.get("Subject", ""), headers.get("Date", "")
    except HttpError:
        return None


# ── Name parsing ──────────────────────────────────────────────────────────────────

def split_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (first_name, last_name)."""
    parts = display_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> str:
    """Infer company name from email domain (skip generic providers)."""
    if domain in SYSTEM_DOMAINS:
        return ""
    # Strip TLD and capitalize: acmecorp.com → Acmecorp
    name = domain.split(".")[0]
    return name.capitalize()


# ── HubSpot helpers ───────────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        raise ValueError("HUBSPOT_ACCESS_TOKEN is not set in environment.")
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(hs: hubspot.Client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    filter_obj = Filter(
        property_name="email",
        operator="EQ",
        value=email,
    )
    fg = FilterGroup(filters=[filter_obj])
    search_req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status", "hs_analytics_source"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(public_object_search_request=search_req)
        if resp.total > 0:
            return resp.results[0]
        return None
    except ContactApiException:
        return None


def build_contact_properties(
    email: str,
    display_name: str,
    domain: str,
    existing: Optional[dict] = None,
) -> dict:
    """
    Build the HubSpot properties dict, only filling in missing fields.
    """
    first_name, last_name = split_name(display_name)
    company = company_from_domain(domain)

    props = {}

    def missing(field: str) -> bool:
        if existing is None:
            return True
        return not existing.properties.get(field)

    if missing("email"):
        props["email"] = email
    if first_name and missing("firstname"):
        props["firstname"] = first_name
    if last_name and missing("lastname"):
        props["lastname"] = last_name
    if company and missing("company"):
        props["company"] = company

    # Always set source
    if missing("hs_analytics_source"):
        props["hs_analytics_source"] = CONTACT_SOURCE

    # Custom source field (safe to always set)
    props["lead_source"] = CONTACT_SOURCE

    return props


def create_contact(hs: hubspot.Client, properties: dict) -> dict:
    """Create a new HubSpot contact."""
    payload = SimplePublicObjectInputForCreate(properties=properties)
    return hs.crm.contacts.basic_api.create(simple_public_object_input_for_create=payload)


def update_contact(hs: hubspot.Client, contact_id: str, properties: dict) -> dict:
    """Update an existing HubSpot contact (patch only changed fields)."""
    from hubspot.crm.contacts import SimplePublicObjectInput
    payload = SimplePublicObjectInput(properties=properties)
    return hs.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input=payload,
    )


def add_email_note(
    hs: hubspot.Client,
    contact_id: str,
    sender_email: str,
    subject: str,
    date_str: str,
) -> None:
    """Add a Note activity on the contact for the received email."""
    body = (
        f"Inbound email received via Gmail\n"
        f"From: {sender_email}\n"
        f"Subject: {subject or '(no subject)'}\n"
        f"Date: {date_str or 'unknown'}\n"
        f"Tag: {INBOUND_TAG}"
    )
    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
    from hubspot.crm.associations import BatchInputPublicAssociation, PublicAssociation

    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    note = NoteCreate(
        properties={
            "hs_note_body": body,
            "hs_timestamp": now_ms,
        }
    )
    try:
        created = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
        # Associate note → contact
        assoc = PublicAssociation(
            from_object_id=created.id,
            to_object_id=contact_id,
            type="note_to_contact",
        )
        hs.crm.associations.batch_api.create(
            from_object_type="notes",
            to_object_type="contacts",
            batch_input_public_association=BatchInputPublicAssociation(inputs=[assoc]),
        )
    except Exception as e:
        log.warning(f"Could not add note for contact {contact_id}: {e}")


# ── Core sync logic ────────────────────────────────────────────────────────────────

RESULT_CREATED = "Creato"
RESULT_UPDATED = "Aggiornato"
RESULT_IGNORED = "Ignorato"


def sync_sender(
    hs: hubspot.Client,
    email: str,
    display_name: str,
    domain: str,
    subject: str = "",
    date_str: str = "",
    add_note: bool = True,
) -> dict:
    """
    Sync a single email sender to HubSpot.
    Returns: {"status": ..., "email": ..., "contact_id": ...}
    """
    existing = find_contact_by_email(hs, email)
    contact_id = None
    status = RESULT_IGNORED

    if existing is None:
        props = build_contact_properties(email, display_name, domain)
        props["email"] = email  # ensure email is always set on create
        try:
            created = create_contact(hs, props)
            contact_id = created.id
            status = RESULT_CREATED
            log.info(f"[{RESULT_CREATED}] {email} → HubSpot ID {contact_id}")
        except ContactApiException as e:
            log.error(f"Failed to create contact {email}: {e}")
            return {"status": "Errore", "email": email, "contact_id": None}
    else:
        contact_id = existing.id
        props = build_contact_properties(email, display_name, domain, existing)
        if props:
            # Remove email from update payload (can't update primary email this way)
            props.pop("email", None)
        if props:
            try:
                update_contact(hs, contact_id, props)
                status = RESULT_UPDATED
                log.info(f"[{RESULT_UPDATED}] {email} → HubSpot ID {contact_id} (fields: {list(props.keys())})")
            except ContactApiException as e:
                log.error(f"Failed to update contact {email}: {e}")
        else:
            status = RESULT_IGNORED
            log.info(f"[{RESULT_IGNORED}] {email} → HubSpot ID {contact_id} (no new fields)")

    if add_note and contact_id and status != RESULT_IGNORED:
        add_email_note(hs, contact_id, email, subject, date_str)

    return {"status": status, "email": email, "contact_id": contact_id}


# ── Main loop ─────────────────────────────────────────────────────────────────────

def run_sync_loop(
    poll_interval: int = POLL_INTERVAL_SECONDS,
    run_once: bool = False,
    add_note: bool = True,
):
    """
    Continuously poll Gmail and sync new senders to HubSpot.

    Args:
        poll_interval: Seconds between Gmail polls.
        run_once: If True, process current emails and exit (useful for cron jobs).
        add_note: If True, add a HubSpot note for each email.
    """
    log.info("Starting Gmail → HubSpot sync")
    gmail_service = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    results_summary = []

    while True:
        log.info(f"Polling Gmail (history_id={state.get('history_id')})…")
        try:
            messages, new_history_id = get_new_messages(gmail_service, state.get("history_id"))
        except Exception as e:
            log.error(f"Gmail polling error: {e}")
            if run_once:
                break
            time.sleep(poll_interval)
            continue

        processed_ids: list = state.get("processed_ids", [])
        new_results = []

        for msg in messages:
            msg_id = msg["id"]
            if msg_id in processed_ids:
                continue

            sender_data = get_message_sender(gmail_service, msg_id)
            if sender_data is None:
                processed_ids.append(msg_id)
                continue

            email, display_name, domain, subject, date_str = sender_data

            # Skip self-sent emails
            profile = gmail_service.users().getProfile(userId="me").execute()
            own_email = profile.get("emailAddress", "").lower()
            if email == own_email:
                processed_ids.append(msg_id)
                continue

            result = sync_sender(
                hs,
                email=email,
                display_name=display_name,
                domain=domain,
                subject=subject,
                date_str=date_str,
                add_note=add_note,
            )
            result["message_id"] = msg_id
            new_results.append(result)
            results_summary.append(result)
            processed_ids.append(msg_id)

        # Keep only the last 10 000 processed IDs to bound memory
        state["processed_ids"] = processed_ids[-10_000:]
        state["history_id"] = new_history_id
        save_state(state)

        if new_results:
            print("\n" + "─" * 60)
            print(f"  Processate {len(new_results)} email")
            print("─" * 60)
            print(f"  {'Stato':<12} {'Email':<35} {'ID HubSpot'}")
            print("─" * 60)
            for r in new_results:
                print(f"  {r['status']:<12} {r['email']:<35} {r.get('contact_id', '—')}")
            print("─" * 60 + "\n")
        else:
            log.info("Nessuna nuova email da processare.")

        if run_once:
            break

        log.info(f"Attendo {poll_interval}s prima del prossimo controllo…")
        time.sleep(poll_interval)

    return results_summary


# ── Entry point ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process current emails once and exit (for cron use).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL_SECONDS,
        help=f"Poll interval in seconds (default: {POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--no-notes",
        action="store_true",
        help="Skip creating HubSpot activity notes.",
    )
    args = parser.parse_args()

    run_sync_loop(
        poll_interval=args.interval,
        run_once=args.once,
        add_note=not args.no_notes,
    )
