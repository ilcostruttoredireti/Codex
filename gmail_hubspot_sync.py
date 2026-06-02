#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
------------------------------
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot CRM.
- Extracts email, name, company (from domain) of each sender
- Creates new HubSpot contacts or updates missing fields on existing ones
- Uses email address as the deduplication key
- Logs an "Incoming Email" engagement timeline activity per contact
- Adds lead source "Gmail" and tag "Inbound Gmail"
- Skips system senders (mailer-daemon, noreply, etc.)
- Persists processed message IDs so re-runs are safe

Setup:
  1. cp .env.example .env  →  fill in HUBSPOT_ACCESS_TOKEN and GMAIL_CREDENTIALS_FILE
  2. pip install -r requirements.txt
  3. python gmail_hubspot_sync.py          # continuous mode (default 5-min interval)
     python gmail_hubspot_sync.py --once  # single run and exit
"""

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import schedule
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import hubspot
from hubspot.crm.contacts import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

# ─── Bootstrap ────────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("gmail_hubspot_sync")

# Gmail OAuth scope (read-only is enough)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path("gmail_token.json")

STATE_FILE = Path("sync_state.json")
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"
SYNC_INTERVAL_MINUTES = int(os.getenv("SYNC_INTERVAL_MINUTES", "5"))

# Senders whose address or domain we always skip
IGNORED_LOCAL_PARTS = frozenset(
    {"noreply", "no-reply", "mailer-daemon", "postmaster", "bounce", "donotreply"}
)
IGNORED_DOMAINS = frozenset(
    {
        "googlemail.com",
        "google.com",
        "bounce.com",
        "sendgrid.net",
        "mailchimp.com",
        "amazonses.com",
        "sg.mailgun.org",
    }
)
# Skip if the local part contains any of these substrings
IGNORED_LOCAL_SUBSTRINGS = ("noreply", "no-reply", "bounce", "daemon")


# ─── State helpers ─────────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log.warning("Corrupt state file — starting fresh")
    return {"processed_message_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ─── Gmail helpers ─────────────────────────────────────────────────────────────


def build_gmail_service():
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def should_skip(email: str) -> bool:
    """Return True for system/noreply addresses that should never become contacts."""
    if not email or "@" not in email:
        return True
    local, domain = email.lower().split("@", 1)
    if domain in IGNORED_DOMAINS:
        return True
    if local in IGNORED_LOCAL_PARTS:
        return True
    if any(sub in local for sub in IGNORED_LOCAL_SUBSTRINGS):
        return True
    return False


def parse_from_header(from_header: str) -> tuple[str, str, str]:
    """Return (email, first_name, last_name) parsed from a raw From header."""
    display_name, addr = parseaddr(from_header)
    addr = addr.lower().strip()

    first_name, last_name = "", ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    return addr, first_name, last_name


def company_from_domain(domain: str) -> str:
    """Best-effort company name: strip common TLDs and capitalise."""
    # Remove known multi-part TLDs like .co.uk
    cleaned = re.sub(r"\.(com|net|org|io|it|co\.\w{2}|co|biz|info|eu|ch|de|fr|es)$", "", domain)
    parts = cleaned.rsplit(".", 1)
    name = parts[-1] if parts else domain
    return name.capitalize()


def fetch_new_messages(service, processed_ids: set) -> list[dict]:
    """Return unread inbox messages not yet in processed_ids."""
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", q="in:inbox is:unread", maxResults=100)
            .execute()
        )
        all_messages = result.get("messages", [])
        new = [m for m in all_messages if m["id"] not in processed_ids]
        log.info(f"Inbox unread: {len(all_messages)} total, {len(new)} new")
        return new
    except HttpError as exc:
        log.error(f"Gmail list error: {exc}")
        return []


def get_sender_info(service, message_id: str) -> Optional[dict]:
    """
    Fetch a single message's metadata and return a sender dict, or None if
    the sender should be skipped.
    """
    try:
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
    except HttpError as exc:
        log.error(f"Gmail get message {message_id}: {exc}")
        return None

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    from_raw = headers.get("From", "")
    email, first_name, last_name = parse_from_header(from_raw)

    if should_skip(email):
        log.debug(f"Skipped sender: {email}")
        return None

    domain = email.split("@")[-1] if "@" in email else ""
    company = company_from_domain(domain) if domain else ""

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
        "subject": headers.get("Subject", "(no subject)"),
        "date": headers.get("Date", ""),
        "message_id": message_id,
    }


# ─── HubSpot helpers ───────────────────────────────────────────────────────────


def build_hubspot_client():
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return hubspot.Client.create(access_token=token)


def find_contact(hs: hubspot.Client, email: str) -> Optional[object]:
    """Search HubSpot for a contact by email. Returns the contact object or None."""
    try:
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(property_name="email", operator="EQ", value=email)
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        resp = hs.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        return resp.results[0] if resp.total > 0 else None
    except ApiException as exc:
        log.error(f"HubSpot search error ({email}): {exc}")
        return None


def create_contact(hs: hubspot.Client, sender: dict) -> Optional[str]:
    """Create a new HubSpot contact and return its ID."""
    props = {
        "email": sender["email"],
        "leadsource": CONTACT_SOURCE,
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
        log.info(f"[CREATED]  {sender['email']}  →  HubSpot ID {result.id}")
        return result.id
    except ApiException as exc:
        log.error(f"HubSpot create error ({sender['email']}): {exc}")
        return None


def update_contact(hs: hubspot.Client, contact_id: str, sender: dict, existing) -> bool:
    """Fill in blank fields on an existing contact. Returns True if any field was updated."""
    ep = existing.properties
    updates = {}

    if sender["first_name"] and not ep.get("firstname"):
        updates["firstname"] = sender["first_name"]
    if sender["last_name"] and not ep.get("lastname"):
        updates["lastname"] = sender["last_name"]
    if sender["company"] and not ep.get("company"):
        updates["company"] = sender["company"]

    if not updates:
        log.info(f"[IGNORED]  {sender['email']}  →  HubSpot ID {contact_id}  (no new data)")
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        log.info(
            f"[UPDATED]  {sender['email']}  →  HubSpot ID {contact_id}"
            f"  fields={list(updates.keys())}"
        )
        return True
    except ApiException as exc:
        log.error(f"HubSpot update error ({sender['email']}): {exc}")
        return False


def log_email_activity(hs: hubspot.Client, contact_id: str, sender: dict) -> None:
    """Create an EMAIL engagement on the contact's timeline."""
    try:
        from hubspot.crm.objects.emails import (
            SimplePublicObjectInputForCreate as EmailInput,
        )

        result = hs.crm.objects.emails.basic_api.create(
            simple_public_object_input_for_create=EmailInput(
                properties={
                    "hs_email_direction": "INCOMING_EMAIL",
                    "hs_email_status": "RECEIVED",
                    "hs_email_subject": sender["subject"],
                    "hs_email_text": (
                        f"Email ricevuta da {sender['email']} il {sender['date']}"
                    ),
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 9,  # EMAIL → CONTACT
                            }
                        ],
                    }
                ],
            )
        )
        log.info(f"[ACTIVITY] Email timeline logged  →  engagement ID {result.id}")
    except Exception as exc:
        # Non-critical: log and continue
        log.warning(f"Could not log email activity for contact {contact_id}: {exc}")


# ─── Core sync logic ───────────────────────────────────────────────────────────


def process_sender(hs: hubspot.Client, sender: dict) -> tuple[str, str, Optional[str]]:
    """
    Upsert a single sender in HubSpot.
    Returns (status, email, hubspot_contact_id).
    Status is one of: "created" | "updated" | "ignored" | "error".
    """
    email = sender["email"]
    existing = find_contact(hs, email)

    if existing is None:
        contact_id = create_contact(hs, sender)
        if contact_id:
            log_email_activity(hs, contact_id, sender)
            return "created", email, contact_id
        return "error", email, None
    else:
        contact_id = existing.id
        updated = update_contact(hs, contact_id, sender, existing)
        log_email_activity(hs, contact_id, sender)
        return ("updated" if updated else "ignored"), email, contact_id


def sync_once() -> list[dict]:
    """One full sync cycle: fetch new Gmail messages, upsert into HubSpot."""
    log.info("━━━ Sync cycle started ━━━")
    state = load_state()
    processed_ids = set(state.get("processed_message_ids", []))

    gmail = build_gmail_service()
    hs = build_hubspot_client()

    messages = fetch_new_messages(gmail, processed_ids)
    results: list[dict] = []

    for msg in messages:
        msg_id = msg["id"]
        processed_ids.add(msg_id)

        sender = get_sender_info(gmail, msg_id)
        if sender is None:
            results.append({"status": "ignored", "email": None, "hubspot_id": None})
            continue

        status, email, hubspot_id = process_sender(hs, sender)
        results.append({"status": status, "email": email, "hubspot_id": hubspot_id})

    state["processed_message_ids"] = sorted(processed_ids)
    save_state(state)

    # Summary
    created = sum(1 for r in results if r["status"] == "created")
    updated = sum(1 for r in results if r["status"] == "updated")
    ignored = sum(1 for r in results if r["status"] == "ignored")
    log.info(
        f"━━━ Sync complete: {created} created / {updated} updated / {ignored} ignored ━━━"
    )

    # Tabular output
    print("\n{'Email':<40} {'Stato':<10} {'HubSpot ID'}")
    print("-" * 65)
    for r in results:
        if r["email"]:
            print(f"{r['email']:<40} {r['status']:<10} {r['hubspot_id'] or '—'}")
    print()

    return results


# ─── Entry point ──────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle then exit (useful for cron)",
    )
    args = parser.parse_args()

    if args.once:
        sync_once()
        return

    log.info(f"Starting continuous sync — interval: {SYNC_INTERVAL_MINUTES} minutes")
    sync_once()
    schedule.every(SYNC_INTERVAL_MINUTES).minutes.do(sync_once)
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
