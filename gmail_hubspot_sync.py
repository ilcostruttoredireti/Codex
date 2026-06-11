#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and creates/updates HubSpot contacts from incoming senders.
"""

import os
import re
import json
import time
import logging
import argparse
from datetime import datetime
from pathlib import Path

# ── Gmail ─────────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── HubSpot ───────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

# ──────────────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_FILE = Path(".gmail_sync_state.json")
LOG_FILE = Path("gmail_hubspot_sync.log")

FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "aol.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "tin.it",
    "fastwebnet.it", "inwind.it", "email.it", "protonmail.com",
}

SKIP_SENDERS = {
    "noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster",
    "bounce", "bounces", "notification", "notifications", "alert",
    "alerts", "newsletter", "unsubscribe", "info-noreply",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
log = logging.getLogger(__name__)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path(os.environ.get("GMAIL_TOKEN_FILE", "token.json"))
    creds_path = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"))

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found: {creds_path}\n"
                    "Download OAuth2 credentials from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def get_hubspot_client():
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable not set.")
    return hubspot.HubSpot(access_token=token)


# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_ids": [], "last_check": None}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_from_header(from_header: str):
    """Return (email, full_name) from a raw From header."""
    from_header = from_header.strip()
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>$', from_header)
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        email = from_header.lower()
        name = ""
    return email, name


def split_name(full_name: str):
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def extract_company_domain(email: str):
    """Return the domain if it looks like a business; empty string otherwise."""
    if "@" not in email:
        return ""
    domain = email.split("@", 1)[1].lower()
    return "" if domain in FREE_DOMAINS else domain


def is_skip_sender(email: str):
    local = email.split("@")[0].lower()
    return any(skip in local for skip in SKIP_SENDERS)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def fetch_inbox_messages(service, after_timestamp_ms: int | None = None):
    """Return list of {id, threadId} for inbox messages since timestamp."""
    query = "in:inbox -from:me -is:draft"
    if after_timestamp_ms:
        after_sec = int(after_timestamp_ms / 1000)
        query += f" after:{after_sec}"

    messages, page_token = [], None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().messages().list(**kwargs).execute()
        messages.extend(resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_headers(service, message_id: str):
    """Return {From, Date, internalDate} for a message."""
    msg = service.users().messages().get(
        userId="me",
        id=message_id,
        format="metadata",
        metadataHeaders=["From", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    return headers.get("From", ""), msg.get("internalDate", "0")


# ── HubSpot ───────────────────────────────────────────────────────────────────

def hs_find_contact(hs: hubspot.HubSpot, email: str):
    """Return (contact_id, properties_dict) or (None, None)."""
    f = Filter(property_name="email", operator="EQ", value=email)
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[f])],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    try:
        result = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
        if result.total > 0:
            c = result.results[0]
            return c.id, c.properties
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
    return None, None


def hs_create_contact(hs: hubspot.HubSpot, email, firstname, lastname, company):
    props = {
        "email": email,
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    try:
        result = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return result.id
    except ApiException as exc:
        log.error("HubSpot create error for %s: %s", email, exc)
        return None


def hs_update_contact(hs: hubspot.HubSpot, contact_id, existing, firstname, lastname, company):
    """Fill only empty fields. Returns True if anything was updated."""
    updates = {}
    if firstname and not existing.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing.get("company"):
        updates["company"] = company

    if not updates:
        return False

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error for %s: %s", contact_id, exc)
        return False


# ── Core logic ────────────────────────────────────────────────────────────────

def process_sender(hs: hubspot.HubSpot, from_header: str):
    """
    Parse the From header, look up / create / update in HubSpot.
    Returns (status, email, contact_id).
    status: 'CREATO' | 'AGGIORNATO' | 'IGNORATO'
    """
    email, full_name = parse_from_header(from_header)

    if not email or "@" not in email:
        return "IGNORATO", email or from_header, None

    if is_skip_sender(email):
        return "IGNORATO", email, None

    firstname, lastname = split_name(full_name)
    company = extract_company_domain(email)

    contact_id, existing = hs_find_contact(hs, email)

    if contact_id:
        updated = hs_update_contact(hs, contact_id, existing, firstname, lastname, company)
        return ("AGGIORNATO" if updated else "IGNORATO"), email, contact_id
    else:
        new_id = hs_create_contact(hs, email, firstname, lastname, company)
        return ("CREATO" if new_id else "IGNORATO"), email, new_id


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(poll_interval: int = 60, once: bool = False):
    log.info("Starting Gmail → HubSpot sync (poll every %ds)", poll_interval)
    gmail = get_gmail_service()
    hs = get_hubspot_client()
    state = load_state()

    while True:
        log.info("Checking inbox…")
        try:
            messages = fetch_inbox_messages(gmail)
            processed = set(state.get("processed_ids", []))
            results = []

            for msg_ref in messages:
                msg_id = msg_ref["id"]
                if msg_id in processed:
                    continue

                try:
                    from_header, _ = get_message_headers(gmail, msg_id)
                except HttpError as exc:
                    log.warning("Could not fetch message %s: %s", msg_id, exc)
                    processed.add(msg_id)
                    continue

                status, email, contact_id = process_sender(hs, from_header)
                processed.add(msg_id)
                results.append((status, email, contact_id))

                row = f"[{status}] {email} → ID: {contact_id}"
                log.info(row)
                print(row)

            state["processed_ids"] = list(processed)
            state["last_check"] = datetime.utcnow().isoformat()
            save_state(state)

            if results:
                print(f"\n{'─'*50}")
                print(f"Processed {len(results)} new message(s)")
                print(f"{'─'*50}\n")

        except Exception as exc:
            log.error("Sync cycle error: %s", exc, exc_info=True)

        if once:
            break

        log.info("Next check in %ds…", poll_interval)
        time.sleep(poll_interval)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Monitor Gmail inbox and sync senders to HubSpot contacts."
    )
    parser.add_argument(
        "--interval", type=int, default=60,
        help="Polling interval in seconds (default: 60)"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single sync pass then exit"
    )
    args = parser.parse_args()
    run(poll_interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
