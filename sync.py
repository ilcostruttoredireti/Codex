#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail and syncs sender contacts to HubSpot CRM.

Usage:
    python sync.py            # Run continuously (polls every POLL_INTERVAL_SECONDS)
    python sync.py --once     # Run a single cycle and exit
    python sync.py --reset    # Clear saved state and rescan from scratch
"""

import argparse
import json
import logging
import os
import re
import sys
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
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.objects.notes import (
    SimplePublicObjectInputForCreate as NoteInputForCreate,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
INITIAL_SCAN_LIMIT = int(os.getenv("INITIAL_SCAN_LIMIT", "100"))

# Generic email provider domains — company name won't be inferred from these
GENERIC_DOMAINS: set[str] = set(
    os.getenv(
        "GENERIC_DOMAINS",
        "gmail.com,yahoo.com,hotmail.com,outlook.com,icloud.com,"
        "protonmail.com,proton.me,live.com,msn.com,aol.com",
    ).split(",")
)

# Senders to always skip (comma-separated emails)
SKIP_SENDERS: set[str] = {
    s.strip().lower()
    for s in os.getenv("SKIP_SENDERS", "").split(",")
    if s.strip()
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("gmail_hubspot")


# ---------------------------------------------------------------------------
# Gmail Authentication
# ---------------------------------------------------------------------------

def build_gmail_service():
    creds: Optional[Credentials] = None
    token_path = Path(GMAIL_TOKEN_FILE)
    creds_path = Path(GMAIL_CREDENTIALS_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Gmail credentials file not found: {GMAIL_CREDENTIALS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials\n"
                    "See README.md for setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
        log.info("Gmail token saved to %s", GMAIL_TOKEN_FILE)

    return build("gmail", "v1", credentials=creds)


# ---------------------------------------------------------------------------
# HubSpot Client
# ---------------------------------------------------------------------------

def build_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_ACCESS_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN is not set.\n"
            "Create a Private App in HubSpot → Settings → Integrations → Private Apps\n"
            "and copy the access token into your .env file."
        )
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


# ---------------------------------------------------------------------------
# State Management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    path = Path(STATE_FILE)
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {"history_id": None, "processed_messages": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Email Parsing Helpers
# ---------------------------------------------------------------------------

_NOREPLY_PATTERN = re.compile(
    r"(no.?reply|donotreply|notifications?|mailer.?daemon|postmaster|bounce|autoreply|autorespond)",
    re.I,
)


def is_automated_sender(email: str) -> bool:
    return bool(_NOREPLY_PATTERN.search(email))


def parse_from_header(from_header: str) -> tuple[str, str, str, str]:
    """
    Parse a RFC 2822 From header.
    Returns (full_name, first_name, last_name, email_address).
    """
    display_name, address = parseaddr(from_header)
    address = address.lower().strip()
    display_name = display_name.strip().strip('"')

    first_name = last_name = ""
    if display_name:
        parts = display_name.split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    return display_name, first_name, last_name, address


def domain_from_email(email: str) -> str:
    return email.split("@")[1].lower() if "@" in email else ""


def company_from_domain(domain: str) -> str:
    """
    Best-effort company name from domain.
    'acme-solutions.co.uk' → 'Acme Solutions'
    """
    if domain in GENERIC_DOMAINS:
        return ""
    # Strip TLD(s) — handles .co.uk, .com, .io, etc.
    parts = domain.split(".")
    name_part = parts[-2] if len(parts) >= 2 else parts[0]
    return re.sub(r"[-_]", " ", name_part).title()


# ---------------------------------------------------------------------------
# Gmail API Calls
# ---------------------------------------------------------------------------

def fetch_message_headers(service, message_id: str) -> dict:
    """Fetch only the metadata headers we need (fast, minimal quota)."""
    msg = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        )
        .execute()
    )
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    headers["_labelIds"] = msg.get("labelIds", [])
    return headers


def get_inbox_messages(service, max_results: int = 100) -> list[dict]:
    """Retrieve recent INBOX messages for the initial scan."""
    result = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=max_results)
        .execute()
    )
    return result.get("messages", [])


def get_history_messages(
    service, start_history_id: str
) -> tuple[list[dict], Optional[str]]:
    """
    Use the Gmail History API to retrieve only messages added since the last
    known history ID.  Returns (messages, new_history_id).
    Returns (None, None) if the history ID has expired (triggers a full rescan).
    """
    messages: list[dict] = []
    try:
        response = (
            service.users()
            .history()
            .list(
                userId="me",
                startHistoryId=start_history_id,
                historyTypes=["messageAdded"],
                labelId="INBOX",
            )
            .execute()
        )
        for record in response.get("history", []):
            for added in record.get("messagesAdded", []):
                msg = added.get("message", {})
                if "INBOX" in msg.get("labelIds", []):
                    messages.append({"id": msg["id"]})
        new_history_id: Optional[str] = response.get("historyId", start_history_id)
    except HttpError as exc:
        if exc.resp.status == 404:
            log.warning("Gmail history ID expired — will rescan recent messages.")
            return None, None  # type: ignore[return-value]
        raise
    return messages, new_history_id


def get_current_history_id(service) -> str:
    profile = service.users().getProfile(userId="me").execute()
    return profile["historyId"]


# ---------------------------------------------------------------------------
# HubSpot API Calls
# ---------------------------------------------------------------------------

def hs_find_contact(client: hubspot.Client, email: str) -> Optional[object]:
    """Return the first HubSpot contact matching the given email, or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return resp.results[0] if resp.results else None
    except ApiException as exc:
        log.error("HubSpot search failed for %s: %s", email, exc)
        return None


def hs_create_contact(client: hubspot.Client, properties: dict) -> Optional[str]:
    """Create a new contact. Returns the new ID, or None on failure."""
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=properties
            )
        )
        return result.id
    except ApiException as exc:
        if exc.status == 409:
            # Race condition — contact was created between our search and create
            existing = hs_find_contact(client, properties["email"])
            return existing.id if existing else None
        log.error("HubSpot create contact failed: %s", exc)
        return None


def hs_update_contact(
    client: hubspot.Client, contact_id: str, properties: dict
) -> bool:
    """Update an existing contact. Returns True on success."""
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=properties),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update contact %s failed: %s", contact_id, exc)
        return False


def hs_add_note(
    client: hubspot.Client,
    contact_id: str,
    subject: str,
    sender_email: str,
    date_str: str,
) -> None:
    """Add an inbound-email note to the contact's timeline."""
    body = (
        f"Inbound Gmail received\n"
        f"From: {sender_email}\n"
        f"Subject: {subject or '(no subject)'}\n"
        f"Date: {date_str}\n"
        f"Tag: Inbound Gmail"
    )
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInputForCreate(
                properties={"hs_note_body": body, "hs_timestamp": timestamp_ms}
            )
        )
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:  # notes API is optional — don't abort on failure
        log.debug("Could not add timeline note to contact %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Core Processing
# ---------------------------------------------------------------------------

def process_message(
    gmail_service,
    hs_client: hubspot.Client,
    message_id: str,
) -> dict:
    """
    Process one Gmail message and sync the sender to HubSpot.

    Returns a result dict:
        {
            "message_id": str,
            "status": "created" | "updated" | "ignored",
            "email": str | None,
            "contact_id": str | None,
        }
    """
    result = {
        "message_id": message_id,
        "status": "ignored",
        "email": None,
        "contact_id": None,
    }

    try:
        headers = fetch_message_headers(gmail_service, message_id)
    except HttpError as exc:
        log.error("Could not fetch message %s: %s", message_id, exc)
        return result

    from_header = headers.get("From", "")
    subject = headers.get("Subject", "")
    date_str = headers.get("Date", "")

    if not from_header:
        return result

    full_name, first_name, last_name, email = parse_from_header(from_header)
    result["email"] = email

    if not email or "@" not in email:
        log.debug("Skipping message %s — no valid email in From header", message_id)
        return result

    if is_automated_sender(email):
        log.debug("Skipping automated sender: %s", email)
        return result

    if email in SKIP_SENDERS:
        log.debug("Skipping excluded sender: %s", email)
        return result

    domain = domain_from_email(email)
    company = company_from_domain(domain)

    # ---- Build HubSpot property map ----
    new_props: dict[str, str] = {"email": email, "hs_lead_source": "Gmail"}
    if first_name:
        new_props["firstname"] = first_name
    if last_name:
        new_props["lastname"] = last_name
    if company:
        new_props["company"] = company

    # ---- Check for existing contact ----
    existing = hs_find_contact(hs_client, email)

    if existing:
        contact_id: str = existing.id
        existing_props: dict = existing.properties or {}

        # Only patch fields that are currently empty
        updates = {
            k: v
            for k, v in new_props.items()
            if k != "email" and v and not existing_props.get(k)
        }

        if updates:
            ok = hs_update_contact(hs_client, contact_id, updates)
            result["status"] = "updated" if ok else "ignored"
        else:
            result["status"] = "ignored"

        result["contact_id"] = contact_id

    else:
        contact_id = hs_create_contact(hs_client, new_props)
        if contact_id:
            result["status"] = "created"
            result["contact_id"] = contact_id

    # ---- Timeline activity ----
    if result["status"] in ("created", "updated") and result["contact_id"]:
        hs_add_note(hs_client, result["contact_id"], subject, email, date_str)

    _log_result(result)
    return result


def _log_result(result: dict) -> None:
    icons = {"created": "✓ CREATO ", "updated": "↑ AGGIORNATO", "ignored": "– IGNORATO"}
    icon = icons.get(result["status"], "?")
    log.info(
        "[%s]  %-40s  HubSpot ID: %s",
        icon,
        result["email"] or "—",
        result["contact_id"] or "—",
    )


# ---------------------------------------------------------------------------
# Sync Cycle
# ---------------------------------------------------------------------------

def run_cycle(gmail_service, hs_client: hubspot.Client, state: dict) -> dict:
    """Execute one sync cycle. Returns the updated state dict."""

    if state.get("history_id") is None:
        log.info("Primo avvio — scansione degli ultimi %d messaggi in INBOX...", INITIAL_SCAN_LIMIT)
        messages = get_inbox_messages(gmail_service, max_results=INITIAL_SCAN_LIMIT)
        new_history_id = get_current_history_id(gmail_service)
    else:
        messages, new_history_id = get_history_messages(gmail_service, state["history_id"])
        if new_history_id is None:
            log.warning("History ID scaduto — rescan recenti...")
            messages = get_inbox_messages(gmail_service, max_results=INITIAL_SCAN_LIMIT)
            new_history_id = get_current_history_id(gmail_service)

    already_seen: set[str] = set(state.get("processed_messages", []))
    new_messages = [m for m in messages if m["id"] not in already_seen]

    if not new_messages:
        log.debug("Nessun nuovo messaggio in questo ciclo.")
    else:
        log.info("%d nuovo/i messaggio/i da elaborare...", len(new_messages))

    counts = {"created": 0, "updated": 0, "ignored": 0}
    for msg in new_messages:
        res = process_message(gmail_service, hs_client, msg["id"])
        counts[res["status"]] += 1
        already_seen.add(msg["id"])

    if new_messages:
        log.info(
            "Ciclo completato — Creati: %d  Aggiornati: %d  Ignorati: %d",
            counts["created"], counts["updated"], counts["ignored"],
        )

    # Keep only the most recent 2000 IDs to bound state file size
    state["processed_messages"] = list(already_seen)[-2000:]
    state["history_id"] = new_history_id
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return state


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once", action="store_true", help="Run a single sync cycle and exit"
    )
    parser.add_argument(
        "--reset", action="store_true", help="Clear saved state before running"
    )
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("   Gmail → HubSpot Contact Sync")
    log.info("=" * 60)

    if args.reset:
        Path(STATE_FILE).unlink(missing_ok=True)
        log.info("State file cleared.")

    try:
        gmail_service = build_gmail_service()
        hs_client = build_hubspot_client()
    except (FileNotFoundError, ValueError) as exc:
        log.error("Configurazione mancante: %s", exc)
        sys.exit(1)

    log.info("Autenticazione riuscita.")

    if args.once:
        state = load_state()
        state = run_cycle(gmail_service, hs_client, state)
        save_state(state)
        log.info("Ciclo singolo completato.")
        return

    log.info("Modalità continua — intervallo: %ds  (Ctrl+C per fermare)", POLL_INTERVAL)
    state = load_state()

    while True:
        try:
            state = run_cycle(gmail_service, hs_client, state)
            save_state(state)
        except KeyboardInterrupt:
            log.info("Fermato dall'utente.")
            break
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
