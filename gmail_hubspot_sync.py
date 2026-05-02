#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Polls Gmail INBOX for new messages, extracts sender information, and
creates or updates the corresponding contact in HubSpot CRM.

Results per email:
  - creato   : new contact was added to HubSpot
  - aggiornato: existing contact was enriched with missing fields
  - ignorato  : sender skipped (no-reply, already complete, or system address)
  - errore    : API call failed
"""

from __future__ import annotations

import json
import logging
import os
import time
import email.utils
from datetime import datetime, timezone
from pathlib import Path

# ── Google / Gmail ────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── HubSpot ───────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
FIRST_RUN_LIMIT = int(os.getenv("FIRST_RUN_LIMIT", "50"))
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))

# Local parts / domains that indicate automated / system senders
_IGNORED_LOCAL = frozenset(
    {
        "noreply",
        "no-reply",
        "do-not-reply",
        "donotreply",
        "bounce",
        "mailer-daemon",
        "postmaster",
        "unsubscribe",
        "notifications",
        "support",
        "admin",
    }
)
_IGNORED_DOMAIN_FRAGMENTS = frozenset(
    {
        "amazonses.com",
        "sendgrid.net",
        "mailchimp.com",
        "mandrillapp.com",
        "sparkpostmail.com",
        "mailjet.com",
        "mailgun.org",
        "mailer.hubspot.com",
        "bounce.mail.hubspot.com",
        "notifications.github.com",
        "noreply.github.com",
    }
)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def build_gmail_service():
    """Authenticate with OAuth2 and return a Gmail API service object."""
    creds: Credentials | None = None

    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {GMAIL_CREDS_FILE}\n"
                    "Download it from Google Cloud Console and set GMAIL_CREDENTIALS_FILE."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_message_headers(gmail, msg_id: str) -> tuple[str, str]:
    """Return (From header, Subject header) for a given message ID."""
    try:
        msg = (
            gmail.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )
            .execute()
        )
        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return headers.get("From", ""), headers.get("Subject", "(no subject)")
    except HttpError as exc:
        log.error("Gmail fetch error for message %s: %s", msg_id, exc)
        return "", ""


def get_new_inbox_message_ids(gmail, state: dict) -> tuple[list[str], str]:
    """
    Use the Gmail History API for incremental polling.
    On the very first run, fall back to listing recent INBOX messages.
    Returns (list_of_message_ids, updated_history_id).
    """
    history_id: str | None = state.get("history_id")

    if not history_id:
        profile = gmail.users().getProfile(userId="me").execute()
        new_history_id: str = profile["historyId"]
        result = (
            gmail.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=FIRST_RUN_LIMIT)
            .execute()
        )
        ids = [m["id"] for m in result.get("messages", [])]
        log.info("First run: fetched %d recent INBOX messages.", len(ids))
        return ids, new_history_id

    try:
        history = (
            gmail.users()
            .history()
            .list(
                userId="me",
                startHistoryId=history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
        new_history_id = history.get("historyId", history_id)
        ids = [
            added["message"]["id"]
            for record in history.get("history", [])
            for added in record.get("messagesAdded", [])
        ]
        return ids, new_history_id
    except HttpError as exc:
        log.error("Gmail History API error: %s", exc)
        return [], history_id


# ── Sender parsing helpers ────────────────────────────────────────────────────

def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Parse an RFC 2822 From header.
    Returns (email_address, first_name, last_name).
    """
    display_name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    first_name = last_name = ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
    return addr, first_name, last_name


def company_from_domain(email_addr: str) -> str:
    """Derive a human-readable company name from the email domain."""
    if "@" not in email_addr:
        return ""
    domain = email_addr.split("@", 1)[1]
    # Take the second-level domain label and title-case it
    # e.g. "mail.acme.co.uk" → "Acme"
    labels = domain.split(".")
    label = labels[-3] if len(labels) >= 3 else labels[0]
    return label.replace("-", " ").title()


def is_ignored(email_addr: str) -> bool:
    """Return True for system / automated sender addresses."""
    if "@" not in email_addr:
        return True
    local, _, domain = email_addr.partition("@")
    if local in _IGNORED_LOCAL:
        return True
    if any(local.startswith(p) for p in ("noreply", "no-reply", "bounce", "mailer-")):
        return True
    if any(frag in domain for frag in _IGNORED_DOMAIN_FRAGMENTS):
        return True
    return False


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def build_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        raise EnvironmentError(
            "HUBSPOT_ACCESS_TOKEN environment variable is not set."
        )
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact(hs: hubspot.Client, email_addr: str):
    """Return the HubSpot contact object or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email_addr)]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        return resp.results[0] if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
        return None


def create_contact(
    hs: hubspot.Client,
    email_addr: str,
    first_name: str,
    last_name: str,
    company: str,
) -> tuple[str, str]:
    """Create a new HubSpot contact. Returns (contact_id, 'creato'|'errore')."""
    props: dict[str, str] = {"email": email_addr, "leadsource": "Gmail"}
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company

    try:
        contact = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return contact.id, "creato"
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", email_addr, exc)
        return "", "errore"


def update_contact(
    hs: hubspot.Client,
    contact_id: str,
    existing_props: dict,
    first_name: str,
    last_name: str,
    company: str,
) -> tuple[str, str]:
    """
    Enrich a HubSpot contact with any fields that are currently blank.
    Returns (contact_id, 'aggiornato'|'ignorato'|'errore').
    """
    updates: dict[str, str] = {}
    if first_name and not existing_props.get("firstname"):
        updates["firstname"] = first_name
    if last_name and not existing_props.get("lastname"):
        updates["lastname"] = last_name
    if company and not existing_props.get("company"):
        updates["company"] = company
    if not existing_props.get("leadsource"):
        updates["leadsource"] = "Gmail"

    if not updates:
        return contact_id, "ignorato"

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return contact_id, "aggiornato"
    except ApiException as exc:
        log.error("HubSpot update error for contact %s: %s", contact_id, exc)
        return contact_id, "errore"


def add_note(
    hs: hubspot.Client,
    contact_id: str,
    email_addr: str,
    subject: str,
) -> None:
    """Attach an inbound-email note to the contact's timeline."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"[Inbound Gmail] Email ricevuta da {email_addr}\n"
        f"Oggetto: {subject}\n"
        f"Tag: Inbound Gmail"
    )
    try:
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": body,
                    "hs_timestamp": timestamp_ms,
                },
                associations=[
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
            )
        )
    except Exception as exc:
        log.warning("Could not create timeline note for contact %s: %s", contact_id, exc)


# ── Core processing ───────────────────────────────────────────────────────────

def process_message(gmail, hs: hubspot.Client, msg_id: str) -> dict:
    """
    Process one Gmail message: extract sender, sync to HubSpot.
    Returns a result dict with keys: stato, email, hubspot_id.
    """
    from_header, subject = fetch_message_headers(gmail, msg_id)
    if not from_header:
        return {"stato": "ignorato", "email": "", "hubspot_id": ""}

    email_addr, first_name, last_name = parse_sender(from_header)
    if not email_addr or "@" not in email_addr:
        return {"stato": "ignorato", "email": email_addr, "hubspot_id": ""}

    if is_ignored(email_addr):
        log.debug("Skipping system/automated sender: %s", email_addr)
        return {"stato": "ignorato", "email": email_addr, "hubspot_id": ""}

    company = company_from_domain(email_addr)
    existing = find_contact(hs, email_addr)

    if existing:
        existing_props = existing.properties or {}
        contact_id, stato = update_contact(
            hs, existing.id, existing_props, first_name, last_name, company
        )
    else:
        contact_id, stato = create_contact(hs, email_addr, first_name, last_name, company)

    if contact_id and stato != "errore":
        add_note(hs, contact_id, email_addr, subject)

    result = {"stato": stato, "email": email_addr, "hubspot_id": contact_id}
    log.info("[%s] %s  →  HubSpot ID: %s", stato.upper(), email_addr, contact_id or "—")
    return result


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupted state file; starting fresh.")
    return {"history_id": None, "processed": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main loop ─────────────────────────────────────────────────────────────────

def run() -> None:
    log.info("Gmail → HubSpot sync started (poll every %ds).", POLL_INTERVAL)
    gmail = build_gmail_service()
    hs = build_hubspot_client()
    state = load_state()
    processed: set[str] = set(state.get("processed", []))

    while True:
        try:
            msg_ids, new_history_id = get_new_inbox_message_ids(gmail, state)
            novel = [m for m in msg_ids if m not in processed]

            if novel:
                log.info("Processing %d new message(s).", len(novel))
                for msg_id in novel:
                    result = process_message(gmail, hs, msg_id)
                    processed.add(msg_id)

            # Trim the seen-set to avoid unbounded growth
            if len(processed) > 10_000:
                processed = set(list(processed)[-5_000:])

            state["history_id"] = new_history_id
            state["processed"] = list(processed)
            save_state(state)

        except KeyboardInterrupt:
            log.info("Stopped by user.")
            save_state(state)
            break
        except Exception as exc:
            log.error("Unhandled error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
