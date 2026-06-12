#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs sender contacts to HubSpot.
"""

import os
import re
import json
import time
import base64
import logging
import argparse
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# Gmail OAuth scopes (read-only inbox)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Domains we skip (no-reply, notifications, etc.)
IGNORED_DOMAINS = {
    "noreply.github.com", "mailer.notion.so", "mail.notion.so",
    "notifications.google.com", "accounts.google.com",
    "no-reply.accounts.google.com",
}
IGNORED_LOCAL_PARTS = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}

HUBSPOT_API_BASE = "https://api.hubapi.com"
PROCESSED_IDS_FILE = os.getenv("PROCESSED_IDS_FILE", ".processed_message_ids.json")


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service(credentials_file: str, token_file: str):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, after_timestamp: Optional[int] = None, max_results: int = 50) -> list[dict]:
    query = "in:inbox -from:me"
    if after_timestamp:
        query += f" after:{after_timestamp}"
    try:
        result = service.users().messages().list(
            userId="me", q=query, maxResults=max_results
        ).execute()
        return result.get("messages", [])
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return []


def get_message_headers(service, message_id: str) -> dict:
    try:
        msg = service.users().messages().get(
            userId="me", id=message_id, format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        headers["_internal_date"] = msg.get("internalDate")
        return headers
    except HttpError as exc:
        log.error("Gmail get message error (id=%s): %s", message_id, exc)
        return {}


# ---------------------------------------------------------------------------
# Contact extraction
# ---------------------------------------------------------------------------

def parse_sender(from_header: str) -> Optional[dict]:
    """
    Parse a 'From' header into {email, first_name, last_name, company, domain}.
    Returns None if the sender should be skipped.
    """
    name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    local, domain = email.split("@", 1)

    if domain in IGNORED_DOMAINS:
        return None
    if local in IGNORED_LOCAL_PARTS:
        return None

    # Derive company from domain (strip common TLDs and www)
    company = _domain_to_company(domain)

    first_name, last_name = _split_name(name.strip())

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


def _domain_to_company(domain: str) -> str:
    """gmail.com → Gmail,  acme-corp.io → Acme Corp"""
    parts = domain.split(".")
    # Drop common public mail providers
    public_providers = {"gmail", "yahoo", "hotmail", "outlook", "live", "icloud", "protonmail", "me"}
    if len(parts) >= 2 and parts[-2].lower() in public_providers:
        return ""
    name = parts[0] if parts else domain
    name = re.sub(r"[-_]", " ", name).title()
    return name


def _split_name(full_name: str) -> tuple[str, str]:
    """'John Doe' → ('John', 'Doe'),  '' → ('', '')"""
    if not full_name:
        return "", ""
    parts = full_name.split(None, 1)  # split on first whitespace
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers() -> dict:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def hubspot_find_contact(email: str) -> Optional[dict]:
    """Return the HubSpot contact dict if found, else None."""
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hubspot_create_contact(contact: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    properties = _build_hs_properties(contact)
    resp = requests.post(url, headers=_hs_headers(), json={"properties": properties}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hubspot_update_contact(contact_id: str, contact: dict, existing_props: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    properties = _build_hs_properties(contact, existing=existing_props)
    if not properties:
        return {"id": contact_id, "_no_changes": True}
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": properties}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hubspot_create_engagement(contact_id: str, subject: str, email: str, received_at: str) -> None:
    """Log an 'email received' activity on the contact's timeline."""
    url = f"{HUBSPOT_API_BASE}/engagements/v1/engagements"
    payload = {
        "engagement": {"active": True, "type": "EMAIL", "timestamp": _to_ms(received_at)},
        "associations": {"contactIds": [int(contact_id)]},
        "metadata": {
            "from": {"email": email},
            "subject": subject,
            "text": f"Inbound email received from {email}",
        },
    }
    try:
        resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Timeline engagement failed for contact %s: %s", contact_id, exc)


def _build_hs_properties(contact: dict, existing: Optional[dict] = None) -> dict:
    """Build only the HubSpot properties that are missing or blank."""
    props: dict = {}
    existing = existing or {}

    def _set_if_missing(hs_key: str, value: str) -> None:
        if value and not existing.get(hs_key):
            props[hs_key] = value

    # Always stamp the source
    props["leadsource"] = "Gmail"
    props["hs_lead_status"] = existing.get("hs_lead_status") or "NEW"

    _set_if_missing("firstname", contact.get("first_name", ""))
    _set_if_missing("lastname", contact.get("last_name", ""))
    _set_if_missing("company", contact.get("company", ""))

    # For new contacts we also set the email
    if "email" not in existing:
        props["email"] = contact["email"]

    return props


def _to_ms(date_str: str) -> int:
    """Convert RFC2822 date string or epoch-ms string to epoch milliseconds."""
    if date_str and date_str.isdigit():
        return int(date_str)
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# Processed-IDs persistence (avoid re-processing on restart)
# ---------------------------------------------------------------------------

def load_processed_ids() -> set[str]:
    if os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE) as fh:
            return set(json.load(fh))
    return set()


def save_processed_ids(ids: set[str]) -> None:
    with open(PROCESSED_IDS_FILE, "w") as fh:
        json.dump(sorted(ids), fh)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_message(service, message_id: str) -> dict:
    """
    Process a single Gmail message.
    Returns a result dict with keys: status, email, hubspot_id.
    """
    headers = get_message_headers(service, message_id)
    if not headers:
        return {"status": "Ignorato", "email": "—", "hubspot_id": "—", "reason": "no headers"}

    from_header = headers.get("From", "")
    subject = headers.get("Subject", "(no subject)")
    date = headers.get("Date", "")

    contact = parse_sender(from_header)
    if not contact:
        return {"status": "Ignorato", "email": from_header, "hubspot_id": "—", "reason": "filtered sender"}

    email = contact["email"]

    existing = hubspot_find_contact(email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        updated = hubspot_update_contact(contact_id, contact, existing_props)
        if updated.get("_no_changes"):
            status = "Aggiornato (nessuna modifica)"
        else:
            status = "Aggiornato"
    else:
        created = hubspot_create_contact(contact)
        contact_id = created["id"]
        status = "Creato"

    # Optional: timeline engagement
    hubspot_create_engagement(contact_id, subject, email, date or headers.get("_internal_date", ""))

    return {"status": status, "email": email, "hubspot_id": contact_id}


def run_sync(service, poll_interval_seconds: int = 60) -> None:
    processed_ids = load_processed_ids()
    last_check = int(time.time()) - poll_interval_seconds  # seconds epoch

    log.info("Starting Gmail → HubSpot sync (poll every %ds). Press Ctrl+C to stop.", poll_interval_seconds)

    while True:
        log.info("Checking Gmail inbox (after=%d)…", last_check)
        messages = fetch_inbox_messages(service, after_timestamp=last_check)
        new_check = int(time.time())

        new_messages = [m for m in messages if m["id"] not in processed_ids]
        if new_messages:
            log.info("Found %d new message(s) to process.", len(new_messages))
        else:
            log.info("No new messages.")

        results = []
        for msg in new_messages:
            try:
                result = process_message(service, msg["id"])
                processed_ids.add(msg["id"])
                results.append(result)
                _print_result(result)
            except Exception as exc:
                log.error("Error processing message %s: %s", msg["id"], exc)

        if results:
            save_processed_ids(processed_ids)

        last_check = new_check
        time.sleep(poll_interval_seconds)


def run_once(service) -> None:
    """Process all unread inbox messages once and exit."""
    processed_ids = load_processed_ids()
    messages = fetch_inbox_messages(service, max_results=100)
    new_messages = [m for m in messages if m["id"] not in processed_ids]

    log.info("Processing %d message(s)…", len(new_messages))
    for msg in new_messages:
        try:
            result = process_message(service, msg["id"])
            processed_ids.add(msg["id"])
            _print_result(result)
        except Exception as exc:
            log.error("Error processing message %s: %s", msg["id"], exc)

    save_processed_ids(processed_ids)
    log.info("Done.")


def _print_result(result: dict) -> None:
    status = result["status"].ljust(30)
    email = result["email"].ljust(40)
    hs_id = result["hubspot_id"]
    reason = f"  ({result['reason']})" if result.get("reason") else ""
    print(f"  {status}  {email}  HubSpot ID: {hs_id}{reason}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts.")
    parser.add_argument("--credentials", default=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
                        help="Path to Gmail OAuth2 credentials JSON (default: credentials.json)")
    parser.add_argument("--token", default=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
                        help="Path to store/load the OAuth2 token (default: token.json)")
    parser.add_argument("--interval", type=int, default=int(os.getenv("POLL_INTERVAL", "60")),
                        help="Poll interval in seconds for continuous mode (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="Process existing inbox messages once and exit")
    args = parser.parse_args()

    if not os.getenv("HUBSPOT_ACCESS_TOKEN"):
        log.error("HUBSPOT_ACCESS_TOKEN environment variable is not set.")
        raise SystemExit(1)

    service = get_gmail_service(args.credentials, args.token)

    if args.once:
        run_once(service)
    else:
        run_sync(service, poll_interval_seconds=args.interval)


if __name__ == "__main__":
    main()
