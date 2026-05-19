"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and creates/updates contacts in HubSpot.
"""

import os
import json
import time
import logging
import re
import base64
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException as ContactApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput
from hubspot.crm.timeline import (
    TimelineEvent,
    TimelineEventTemplateToken,
    ApiException as TimelineApiException,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ─── Configuration ────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")

# Domains to ignore (transactional / no-reply senders)
IGNORED_DOMAINS = {
    "noreply.com", "no-reply.com", "notifications.google.com",
    "mailer-daemon", "amazonses.com", "sendgrid.net",
    "mailchimp.com", "mandrillapp.com",
}
IGNORED_LOCAL_PARTS = {"noreply", "no-reply", "mailer-daemon", "bounce", "postmaster", "donotreply"}

# ─── State persistence ────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_history_id": None, "processed_message_ids": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ─── Gmail helpers ────────────────────────────────────────────────────────────

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
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_inbox_messages(service, state: dict) -> list[dict]:
    """Return inbox messages not yet processed, newest-first."""
    processed_ids: set = set(state.get("processed_message_ids", []))
    results = (
        service.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], maxResults=50)
        .execute()
    )
    messages = results.get("messages", [])
    new_messages = [m for m in messages if m["id"] not in processed_ids]
    return new_messages


def get_message_detail(service, message_id: str) -> Optional[dict]:
    """Return a simplified dict with from_email, from_name, subject."""
    try:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=message_id, format="metadata",
                 metadataHeaders=["From", "Subject", "Date"])
            .execute()
        )
    except Exception as exc:
        log.warning("Could not fetch message %s: %s", message_id, exc)
        return None

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    raw_from = headers.get("From", "")
    from_name, from_email = parseaddr(raw_from)
    from_email = from_email.lower().strip()

    if not from_email or "@" not in from_email:
        return None

    return {
        "message_id": message_id,
        "from_email": from_email,
        "from_name": from_name.strip() or "",
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
    }

# ─── Sender filtering ─────────────────────────────────────────────────────────

def should_ignore(email: str) -> bool:
    local, _, domain = email.partition("@")
    if domain in IGNORED_DOMAINS:
        return True
    if local in IGNORED_LOCAL_PARTS:
        return True
    if any(p in local for p in ("noreply", "no-reply", "bounce", "postmaster")):
        return True
    return False


def parse_name(raw_name: str) -> tuple[str, str]:
    """Split a display name into (first, last). Best-effort."""
    raw_name = raw_name.strip().strip('"')
    parts = raw_name.split(maxsplit=1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def domain_to_company(email: str) -> str:
    """Convert 'user@acme.com' → 'acme.com' as a company hint."""
    domain = email.split("@")[-1]
    # Strip common free email providers
    free_providers = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "icloud.com", "live.com", "aol.com", "protonmail.com",
        "libero.it", "alice.it", "virgilio.it",
    }
    return "" if domain in free_providers else domain

# ─── HubSpot helpers ─────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Return HubSpot contact dict (id + properties) or None."""
    from hubspot.crm.contacts import Filter, FilterGroup, PublicObjectSearchRequest

    filter_ = Filter(property_name="email", operator="EQ", value=email)
    filter_group = FilterGroup(filters=[filter_])
    search_req = PublicObjectSearchRequest(
        filter_groups=[filter_group],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        if resp.total > 0:
            c = resp.results[0]
            return {"id": c.id, "properties": c.properties}
    except ContactApiException as exc:
        log.error("HubSpot search failed for %s: %s", email, exc)
    return None


def create_contact(client: hubspot.Client, props: dict) -> Optional[str]:
    """Create a new HubSpot contact; return its ID or None on failure."""
    try:
        obj = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return obj.id
    except ContactApiException as exc:
        # 409 = already exists (race condition)
        if exc.status == 409:
            log.warning("Contact %s already exists (race condition).", props.get("email"))
        else:
            log.error("Could not create contact %s: %s", props.get("email"), exc)
    return None


def update_contact(client: hubspot.Client, contact_id: str, props: dict) -> bool:
    """Update an existing HubSpot contact. Return True on success."""
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ContactApiException as exc:
        log.error("Could not update contact %s: %s", contact_id, exc)
        return False


def log_email_activity(client: hubspot.Client, contact_id: str, msg: dict) -> None:
    """Create a HubSpot NOTE on the contact recording the inbound email."""
    note_body = (
        f"Inbound Gmail received\n"
        f"Subject: {msg['subject']}\n"
        f"Date: {msg['date']}\n"
        f"From: {msg['from_name']} <{msg['from_email']}>"
    )
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteCreate,
        )
        now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        note_props = {
            "hs_note_body": note_body,
            "hs_timestamp": now_ms,
        }
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(properties=note_props)
        )
        # Associate note → contact
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
        log.info("  ↳ Activity note logged (note_id=%s)", note.id)
    except Exception as exc:
        log.warning("  ↳ Could not log activity note: %s", exc)

# ─── Core sync logic ──────────────────────────────────────────────────────────

def sync_message(client: hubspot.Client, msg: dict) -> dict:
    """
    Process one inbound email message.
    Returns a result dict: {status, email, contact_id}
    """
    email = msg["from_email"]
    result = {"email": email, "status": "ignored", "contact_id": None}

    if should_ignore(email):
        log.info("IGNORED  %s (filtered sender)", email)
        return result

    first, last = parse_name(msg["from_name"])
    company = domain_to_company(email)

    existing = find_contact_by_email(client, email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing["properties"]

        # Build update payload with only missing/empty fields
        update_props = {}
        if first and not existing_props.get("firstname"):
            update_props["firstname"] = first
        if last and not existing_props.get("lastname"):
            update_props["lastname"] = last
        if company and not existing_props.get("company"):
            update_props["company"] = company
        if not existing_props.get("hs_lead_source"):
            update_props["hs_lead_source"] = "Gmail"

        if update_props:
            ok = update_contact(client, contact_id, update_props)
            status = "updated" if ok else "error"
        else:
            status = "unchanged"

        result.update(status=status, contact_id=contact_id)
        log.info("%-9s  %s  (id=%s)", status.upper(), email, contact_id)

    else:
        new_props = {
            "email": email,
            "hs_lead_source": "Gmail",
        }
        if first:
            new_props["firstname"] = first
        if last:
            new_props["lastname"] = last
        if company:
            new_props["company"] = company

        contact_id = create_contact(client, new_props)
        if contact_id:
            result.update(status="created", contact_id=contact_id)
            log.info("CREATED   %s  (id=%s)", email, contact_id)
        else:
            result.update(status="error")
            log.warning("ERROR     %s  (creation failed)", email)

    # Log activity note for created/updated contacts
    if result["contact_id"] and result["status"] in ("created", "updated"):
        log_email_activity(client, result["contact_id"], msg)

    return result

# ─── Main loop ────────────────────────────────────────────────────────────────

def run_sync_loop() -> None:
    log.info("Starting Gmail → HubSpot sync (interval=%ds)", POLL_INTERVAL_SECONDS)

    gmail_service = get_gmail_service()
    hs_client = get_hubspot_client()
    state = load_state()

    while True:
        log.info("─── Polling Gmail inbox ───────────────────────────────")
        new_messages = fetch_new_inbox_messages(gmail_service, state)
        log.info("Found %d new message(s).", len(new_messages))

        processed_ids: list = state.get("processed_message_ids", [])
        results = []

        for m in new_messages:
            detail = get_message_detail(gmail_service, m["id"])
            if detail:
                result = sync_message(hs_client, detail)
                results.append(result)

            # Mark as processed regardless of outcome to avoid re-processing
            processed_ids.append(m["id"])

        # Keep only the last 1000 IDs to bound state file size
        state["processed_message_ids"] = processed_ids[-1000:]
        save_state(state)

        if results:
            log.info("─── Summary ──────────────────────────────────────────")
            log.info("%-9s  %-40s  %s", "Status", "Email", "HubSpot ID")
            log.info("%-9s  %-40s  %s", "-" * 9, "-" * 40, "-" * 12)
            for r in results:
                log.info(
                    "%-9s  %-40s  %s",
                    r["status"].upper(),
                    r["email"],
                    r["contact_id"] or "—",
                )

        log.info("Sleeping %ds before next poll…", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_sync_loop()
