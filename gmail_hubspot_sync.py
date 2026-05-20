#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and upserts senders as HubSpot contacts.
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException as ContactsApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
MAX_MESSAGES_PER_POLL = int(os.getenv("MAX_MESSAGES_PER_POLL", "50"))

# Automated / system senders to skip
_SKIP_RE = re.compile(
    r"(no.?reply|noreply|mailer.daemon|postmaster|bounce|donotreply"
    r"|notifications?@|alerts?@|autoconfirm@|auto-confirm@|do.not.reply@)",
    re.IGNORECASE,
)

# Domains that are free consumer email providers (company name extraction skipped)
_GENERIC_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com",
        "outlook.com", "live.com", "icloud.com", "me.com", "mac.com",
        "aol.com", "protonmail.com", "proton.me",
    }
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Sync State ────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"processed_ids": []}


def save_state(state: dict) -> None:
    # Cap stored IDs to avoid unbounded growth
    if len(state["processed_ids"]) > 10_000:
        state["processed_ids"] = state["processed_ids"][-5_000:]
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def build_gmail_service():
    creds: Optional[Credentials] = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_message_ids(service, processed: set) -> list:
    """Return inbox message IDs not yet processed, newest first."""
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], maxResults=MAX_MESSAGES_PER_POLL)
            .execute()
        )
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return []
    return [m["id"] for m in result.get("messages", []) if m["id"] not in processed]


def get_sender_info(service, message_id: str) -> Optional[dict]:
    """Return {'email', 'name', 'message_id'} parsed from From header, or None."""
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From"],
            )
            .execute()
        )
    except HttpError as exc:
        log.warning("Failed to fetch message %s: %s", message_id, exc)
        return None

    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "")
    if not raw_from:
        return None

    name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.lower().strip()
    if not email_addr or "@" not in email_addr:
        return None

    return {"email": email_addr, "name": name.strip(), "message_id": message_id}


# ── Contact Data Extraction ───────────────────────────────────────────────────

def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """'acme.com' → 'Acme', 'mail.company.co.uk' → 'Company'."""
    for prefix in ("mail.", "email.", "smtp.", "m.", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    # Strip TLD(s)
    parts = domain.split(".")
    # Handle double TLDs like .co.uk, .com.br
    if len(parts) >= 3 and parts[-2] in ("co", "com", "net", "org", "gov", "edu"):
        return parts[-3].capitalize()
    return parts[0].capitalize() if parts else domain.capitalize()


def build_contact_data(sender: dict) -> dict:
    email = sender["email"]
    domain = email.split("@")[1]

    firstname, lastname = _split_name(sender["name"])

    # Fallback: derive from local part  (john.doe@… → John / Doe)
    if not firstname:
        local = email.split("@")[0]
        parts = re.split(r"[._\-+]", local, maxsplit=1)
        firstname = parts[0].capitalize()
        lastname = parts[1].capitalize() if len(parts) > 1 else ""

    company = "" if domain in _GENERIC_DOMAINS else _company_from_domain(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
    }


def should_skip(email: str) -> bool:
    return bool(_SKIP_RE.search(email))


# ── HubSpot ───────────────────────────────────────────────────────────────────

def build_hubspot_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact(hs: hubspot.Client, email: str):
    """Return existing HubSpot contact object or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email)]
            )
        ],
        properties=["email", "firstname", "lastname", "company"],
        limit=1,
    )
    try:
        result = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
        if result.results:
            return result.results[0]
    except ContactsApiException as exc:
        log.error("HubSpot search error: %s", exc)
    return None


def create_contact(hs: hubspot.Client, data: dict) -> Optional[str]:
    props = {k: v for k, v in {
        "email": data["email"],
        "firstname": data["firstname"],
        "lastname": data["lastname"],
        "company": data["company"],
        "leadsource": "EMAIL_MARKETING",  # closest standard value for Gmail
        "hs_lead_status": "NEW",
    }.items() if v}

    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except ContactsApiException as exc:
        log.error("HubSpot create error (%s): %s", data["email"], exc)
        return None


def update_contact_fields(hs: hubspot.Client, contact_id: str, data: dict, existing) -> bool:
    """Fill only missing fields. Returns True if any field was updated."""
    ep = existing.properties
    updates = {}
    if not ep.get("firstname") and data["firstname"]:
        updates["firstname"] = data["firstname"]
    if not ep.get("lastname") and data["lastname"]:
        updates["lastname"] = data["lastname"]
    if not ep.get("company") and data["company"]:
        updates["company"] = data["company"]

    if not updates:
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ContactsApiException as exc:
        log.error("HubSpot update error (%s): %s", contact_id, exc)
        return False


def log_activity(hs: hubspot.Client, contact_id: str, email: str) -> None:
    """Create an 'Inbound Gmail' note on the contact timeline."""
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"📥 Inbound Gmail\n"
        f"Email ricevuta da: {email}\n"
        f"Fonte: Gmail\n"
        f"Tag: Inbound Gmail"
    )
    try:
        hs.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": now_ms},
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
        log.warning("Could not log activity for contact %s: %s", contact_id, exc)


# ── Per-Message Processing ────────────────────────────────────────────────────

def process_message(gmail_service, hs: hubspot.Client, message_id: str) -> dict:
    """
    Returns:
        {"status": "created"|"updated"|"skipped",
         "email": str,
         "contact_id": str|None,
         "reason": str}   # reason present only on skipped
    """
    sender = get_sender_info(gmail_service, message_id)
    if not sender:
        return {"status": "skipped", "email": "—", "contact_id": None, "reason": "no sender header"}

    email = sender["email"]

    if should_skip(email):
        return {"status": "skipped", "email": email, "contact_id": None, "reason": "automated sender"}

    data = build_contact_data(sender)
    existing = find_contact(hs, email)

    if existing:
        contact_id = existing.id
        updated = update_contact_fields(hs, contact_id, data, existing)
        log_activity(hs, contact_id, email)
        return {
            "status": "updated",
            "email": email,
            "contact_id": contact_id,
            "fields_changed": updated,
        }

    contact_id = create_contact(hs, data)
    if contact_id:
        log_activity(hs, contact_id, email)
        return {"status": "created", "email": email, "contact_id": contact_id}

    return {"status": "skipped", "email": email, "contact_id": None, "reason": "creation failed"}


# ── Main Loop ─────────────────────────────────────────────────────────────────

def print_result(result: dict) -> None:
    icon = {"created": "✚", "updated": "↺", "skipped": "–"}.get(result["status"], "?")
    detail = ""
    if result.get("reason"):
        detail = f"  ({result['reason']})"
    elif result["status"] == "updated" and not result.get("fields_changed"):
        detail = "  (già aggiornato, attività loggata)"
    log.info(
        "%s %-8s | %-42s | ID: %s%s",
        icon,
        result["status"].upper(),
        result["email"],
        result.get("contact_id") or "—",
        detail,
    )


def run() -> None:
    if not HUBSPOT_TOKEN:
        raise SystemExit("Errore: imposta HUBSPOT_ACCESS_TOKEN nel file .env")
    if not Path(CREDENTIALS_FILE).exists():
        raise SystemExit(f"Errore: file credenziali Google non trovato: {CREDENTIALS_FILE}")

    gmail_service = build_gmail_service()
    hs = build_hubspot_client()
    state = load_state()
    processed = set(state["processed_ids"])

    log.info("Gmail→HubSpot sync avviato | polling ogni %ds", POLL_INTERVAL)

    while True:
        new_ids = fetch_new_message_ids(gmail_service, processed)

        if new_ids:
            log.info("Trovati %d nuovi messaggi da processare.", len(new_ids))

        for msg_id in new_ids:
            result = process_message(gmail_service, hs, msg_id)
            print_result(result)

            processed.add(msg_id)
            state["processed_ids"].append(msg_id)
            save_state(state)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
