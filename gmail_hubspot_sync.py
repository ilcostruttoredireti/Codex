#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail messages and upserts senders as HubSpot contacts.

Output per email processed:
  Stato: Creato | Aggiornato | Ignorato
  Email contatto
  ID contatto HubSpot
"""

import json
import logging
import os
import re
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
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicObjectInput

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Automated / no-reply sender patterns to skip
_IGNORED_LOCAL = frozenset(
    {"noreply", "no-reply", "donotreply", "bounce", "mailer-daemon", "postmaster", "abuse", "notifications"}
)
_IGNORED_DOMAIN_RE = re.compile(
    r"(bounce|mailer|noreply|no-reply|notifications)\.", re.IGNORECASE
)

# Common free/generic domains — company name cannot be inferred from these
_GENERIC_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
        "outlook.com", "hotmail.com", "hotmail.it", "live.com",
        "icloud.com", "me.com", "mac.com", "protonmail.com",
        "libero.it", "tiscali.it", "alice.it", "virgilio.it",
    }
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    return {"history_id": None, "processed": []}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service():
    creds: Credentials | None = None
    token_path = Path(GMAIL_TOKEN_FILE)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {GMAIL_CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds)


def _get_profile_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return str(profile["historyId"])


def fetch_new_messages(service, state: dict) -> list[dict]:
    """
    Return list of {id, threadId} dicts for new INBOX messages.
    Updates state['history_id'] in-place.
    """
    history_id: str | None = state.get("history_id")
    messages: list[dict] = []

    if history_id:
        try:
            resp = (
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
            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    m = added.get("message", {})
                    if "INBOX" in m.get("labelIds", []):
                        messages.append({"id": m["id"]})
            state["history_id"] = str(resp.get("historyId", history_id))
            return messages
        except HttpError as exc:
            if exc.status_code in (404, 400):
                # historyId expired; fall through to full scan
                log.warning("historyId expired, performing full INBOX scan.")
                state["history_id"] = None
            else:
                raise

    # First run or expired history: scan recent INBOX messages
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=50)
        .execute()
    )
    messages = result.get("messages", [])
    state["history_id"] = _get_profile_history_id(service)
    return messages


def _parse_from_header(raw: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) from a raw From header value."""
    display_name, email = parseaddr(raw)
    email = email.lower().strip()
    first_name, last_name = "", ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""
    return email, first_name, last_name


def _domain(email: str) -> str:
    return email.split("@", 1)[-1] if "@" in email else ""


def _company_from_domain(domain: str) -> str:
    """Derive a best-effort company name from an email domain."""
    if domain in _GENERIC_DOMAINS:
        return ""
    # Drop leading subdomains (mail., smtp., m., mx.)
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in ("mail", "smtp", "m", "mx", "email"):
        parts = parts[1:]
    sld = parts[0] if parts else domain
    return sld.replace("-", " ").replace("_", " ").title()


def _should_ignore(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    if local in _IGNORED_LOCAL:
        return True
    if _IGNORED_DOMAIN_RE.search(domain):
        return True
    return False


def get_sender_data(service, message_id: str) -> dict | None:
    """
    Fetch a Gmail message header and return a sender data dict, or None if
    the message should be ignored.
    """
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Date", "Subject"],
            )
            .execute()
        )
    except HttpError as exc:
        log.warning("Could not fetch message %s: %s", message_id, exc)
        return None

    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "")
    email, first_name, last_name = _parse_from_header(raw_from)

    if _should_ignore(email):
        log.debug("Ignoring automated sender: %s", email or raw_from)
        return None

    domain = _domain(email)
    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": _company_from_domain(domain),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "message_id": message_id,
    }


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        raise RuntimeError(
            "HUBSPOT_ACCESS_TOKEN is not set. Add it to your .env file."
        )
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact(hs: hubspot.Client, email: str):
    """Return existing contact object or None."""
    try:
        return hs.crm.contacts.basic_api.get_by_id(
            contact_id=email,
            id_property="email",
            properties=["email", "firstname", "lastname", "company"],
        )
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise


def _build_props(sender: dict, existing) -> dict:
    """
    Build a HubSpot properties dict for the sender.
    For existing contacts, only fill fields that are currently empty.
    """
    props: dict = {"hs_lead_source": "Gmail"}

    def fill(field: str, value: str) -> None:
        if not value:
            return
        if existing and (existing.properties.get(field) or "").strip():
            return  # preserve existing data
        props[field] = value

    fill("firstname", sender["first_name"])
    fill("lastname", sender["last_name"])
    fill("company", sender["company"])
    return props


def create_contact(hs: hubspot.Client, sender: dict) -> str:
    props = _build_props(sender, None)
    props["email"] = sender["email"]
    obj = SimplePublicObjectInputForCreate(properties=props, associations=[])
    result = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=obj
    )
    _apply_tag(hs, result.id)
    return result.id


def update_contact(hs: hubspot.Client, existing, sender: dict) -> str:
    props = _build_props(sender, existing)
    if props:
        hs.crm.contacts.basic_api.update(
            contact_id=existing.id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
    _apply_tag(hs, existing.id)
    return existing.id


def _apply_tag(hs: hubspot.Client, contact_id: str) -> None:
    """
    Write 'Inbound Gmail' to the custom property 'contact_source_tag'.
    If the property doesn't exist in the portal this call is silently skipped.
    """
    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(
                properties={"contact_source_tag": "Inbound Gmail"}
            ),
        )
    except ApiException:
        pass


def log_email_activity(hs: hubspot.Client, contact_id: str, sender: dict) -> None:
    """Create a Note on the contact timeline recording the received email."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    note_body = (
        f"Email ricevuta via Gmail\n"
        f"Oggetto: {sender['subject']}\n"
        f"Data: {sender['date']}\n"
        f"Fonte: Inbound Gmail"
    )
    try:
        note = hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": timestamp_ms,
                }
            }
        )
        # Associate note → contact (associationTypeId 202 = note to contact)
        hs.crm.associations.v4.basic_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type_id=202,
        )
    except Exception as exc:
        log.debug("Activity log skipped for %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_message(gmail, hs: hubspot.Client, message_id: str) -> dict:
    outcome = {"status": "Ignorato", "email": None, "contact_id": None}

    sender = get_sender_data(gmail, message_id)
    if not sender:
        return outcome

    outcome["email"] = sender["email"]
    email = sender["email"]

    existing = find_contact(hs, email)

    if existing:
        contact_id = update_contact(hs, existing, sender)
        outcome.update(status="Aggiornato", contact_id=contact_id)
        log.info("[Aggiornato] %s  →  HubSpot ID %s", email, contact_id)
    else:
        try:
            contact_id = create_contact(hs, sender)
            outcome.update(status="Creato", contact_id=contact_id)
            log.info("[Creato]     %s  →  HubSpot ID %s", email, contact_id)
        except ApiException as exc:
            if exc.status == 409:
                # Race condition: contact was created between find and create
                body = json.loads(exc.body or "{}")
                contact_id = body.get("message", "").split(":")[-1].strip() or "unknown"
                outcome.update(status="Aggiornato", contact_id=contact_id)
                log.info("[Aggiornato-race] %s  →  HubSpot ID %s", email, contact_id)
            else:
                raise

    if outcome["contact_id"]:
        log_email_activity(hs, outcome["contact_id"], sender)

    return outcome


def _print_result(r: dict) -> None:
    print(
        f"Stato: {r['status']:12s} | "
        f"Email: {r['email'] or 'N/A':40s} | "
        f"HubSpot ID: {r['contact_id'] or 'N/A'}"
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    log.info("Avvio Gmail → HubSpot Sync  (polling ogni %ds)", POLL_INTERVAL)

    gmail = build_gmail_service()
    hs = build_hubspot_client()
    state = _load_state()

    while True:
        try:
            messages = fetch_new_messages(gmail, state)
            log.info("%d messaggio/i nuovo/i trovato/i.", len(messages))

            processed: list[str] = state.setdefault("processed", [])
            processed_set: set[str] = set(processed)

            for msg in messages:
                mid = msg["id"]
                if mid in processed_set:
                    continue
                try:
                    result = process_message(gmail, hs, mid)
                    _print_result(result)
                except Exception as exc:
                    log.error("Errore elaborazione messaggio %s: %s", mid, exc)
                finally:
                    processed_set.add(mid)

            # Persist only the last 10 000 IDs to bound file size
            state["processed"] = list(processed_set)[-10_000:]
            _save_state(state)

        except Exception as exc:
            log.error("Errore ciclo sync: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
