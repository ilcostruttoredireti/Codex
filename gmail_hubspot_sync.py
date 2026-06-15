#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for incoming emails, extracts sender info,
and creates/updates contacts in HubSpot avoiding duplicates.

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client hubspot-api-client

Setup:
    1. Enable Gmail API in Google Cloud Console
    2. Download OAuth2 credentials as credentials.json
    3. Set HUBSPOT_ACCESS_TOKEN env var (Private App token from HubSpot)
    4. Run once interactively to complete OAuth flow: python gmail_hubspot_sync.py --auth
    5. Schedule with cron or run continuously: python gmail_hubspot_sync.py
"""

import os
import re
import json
import time
import email
import base64
import logging
import argparse
from datetime import datetime, timedelta, timezone
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = "token.json"
CREDENTIALS_FILE = "credentials.json"
STATE_FILE = "last_sync.json"

# Senders to skip (internal, notifications, bounces)
SKIP_SENDERS = {
    "mailer-daemon@googlemail.com",
    "no-reply@accounts.google.com",
    "noreply@google.com",
    "notification@priority.facebookmail.com",
    "notifications-noreply@linkedin.com",
    "postmaster@",
}

SKIP_DOMAINS = {
    "facebookmail.com",
    "linkedin.com",
    "bounce.com",
    "amazonses.com",
}


# ──────────────────────────── Gmail helpers ────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_last_sync() -> Optional[str]:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
            return data.get("last_message_id")
    return None


def save_last_sync(message_id: str):
    with open(STATE_FILE, "w") as f:
        json.dump({"last_message_id": message_id, "ts": datetime.now(timezone.utc).isoformat()}, f)


def parse_sender(raw_from: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From header value."""
    parsed = email.headerregistry.Address(addr_spec=raw_from)
    # Handle "Name <email>" format
    match = re.match(r'^"?([^"<]+?)"?\s*<([^>]+)>$', raw_from.strip())
    if match:
        return match.group(1).strip(), match.group(2).strip().lower()
    # Plain email
    addr = re.search(r'[\w.+-]+@[\w.-]+\.\w+', raw_from)
    if addr:
        return "", addr.group(0).lower()
    return "", raw_from.strip().lower()


def should_skip(email_addr: str) -> bool:
    if not email_addr or "@" not in email_addr:
        return True
    domain = email_addr.split("@", 1)[1]
    if any(email_addr.startswith(prefix) for prefix in SKIP_SENDERS):
        return True
    if domain in SKIP_DOMAINS:
        return True
    # Skip generic automated senders
    local = email_addr.split("@")[0]
    if local in {"noreply", "no-reply", "donotreply", "postmaster", "bounce", "mailer-daemon"}:
        return True
    return False


def fetch_new_messages(service, since_message_id: Optional[str] = None, max_results: int = 100):
    """Yield (message_id, sender_name, sender_email) for new inbox messages."""
    query = "in:inbox -from:me -category:spam"
    results = service.users().messages().list(
        userId="me", q=query, maxResults=max_results
    ).execute()

    messages = results.get("messages", [])
    if not messages:
        return

    new_messages = []
    for msg in messages:
        if since_message_id and msg["id"] == since_message_id:
            break
        new_messages.append(msg["id"])

    # Process in chronological order (oldest first)
    for msg_id in reversed(new_messages):
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["From", "Subject", "Date"]
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            subject = headers.get("Subject", "")
            name, addr = parse_sender(raw_from)
            if not should_skip(addr):
                yield msg_id, name, addr, subject
        except HttpError as e:
            log.warning("Could not fetch message %s: %s", msg_id, e)


# ──────────────────────────── HubSpot helpers ─────────────────────────

def get_hubspot_client():
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable not set")
    return hubspot.Client.create(access_token=token)


def extract_name_parts(display_name: str, email_addr: str) -> tuple[str, str]:
    """Return (firstname, lastname) from display name or email local part."""
    if display_name:
        parts = display_name.strip().split(None, 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""
        return firstname, lastname
    local = email_addr.split("@")[0]
    # Convert local part like "mario.rossi" → Mario, Rossi
    parts = re.split(r"[._-]", local)
    if len(parts) >= 2 and all(p.isalpha() for p in parts[:2]):
        return parts[0].capitalize(), parts[1].capitalize()
    return local.capitalize(), ""


def extract_company(email_addr: str) -> str:
    """Derive a company name from the email domain (non-gmail/yahoo/etc.)."""
    domain = email_addr.split("@", 1)[1]
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it",
                "alice.it", "virgilio.it", "tiscali.it", "icloud.com", "me.com"}
    if domain in generic:
        return ""
    # Strip TLD and make readable: "comune.sanseverinomarche.mc.it" → "Comune Sanseverinomarche"
    parts = domain.rstrip(".").split(".")
    # Drop last 1-2 TLD segments
    meaningful = [p for p in parts[:-2] if len(p) > 2] or parts[:1]
    return " ".join(p.capitalize() for p in meaningful)


def find_contact_by_email(hs: hubspot.Client, email_addr: str) -> Optional[dict]:
    """Return the HubSpot contact dict if found, else None."""
    f = Filter(property_name="email", operator="EQ", value=email_addr)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total > 0:
        return resp.results[0]
    return None


def create_contact(hs: hubspot.Client, email_addr: str, firstname: str,
                   lastname: str, company: str) -> str:
    props = {
        "email": email_addr,
        "hs_lead_status": "NEW",
        "lead_source": "Gmail",      # maps to leadsource in many portals
        "hs_analytics_source": "OTHER",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    obj = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(properties=props)
    )
    return obj.id


def update_contact(hs: hubspot.Client, contact_id: str, existing: dict,
                   firstname: str, lastname: str, company: str):
    """Fill in only missing fields on an existing contact."""
    updates = {}
    ep = existing.properties
    if firstname and not ep.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not ep.get("lastname"):
        updates["lastname"] = lastname
    if company and not ep.get("company"):
        updates["company"] = company
    if updates:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                properties=updates
            ),
        )
    return updates


# ──────────────────────────── Main loop ───────────────────────────────

def sync_once(gmail_svc, hs: hubspot.Client, since_id: Optional[str]) -> tuple[list, Optional[str]]:
    results = []
    latest_id = since_id

    for msg_id, sender_name, sender_email, subject in fetch_new_messages(gmail_svc, since_id):
        latest_id = msg_id
        firstname, lastname = extract_name_parts(sender_name, sender_email)
        company = extract_company(sender_email)

        try:
            existing = find_contact_by_email(hs, sender_email)
            if existing:
                updates = update_contact(hs, existing.id, existing, firstname, lastname, company)
                status = "Aggiornato" if updates else "Ignorato (già completo)"
                contact_id = existing.id
            else:
                contact_id = create_contact(hs, sender_email, firstname, lastname, company)
                status = "Creato"
        except ApiException as e:
            status = f"ERRORE: {e.status}"
            contact_id = "N/A"
            log.error("HubSpot API error for %s: %s", sender_email, e)

        entry = {
            "stato": status,
            "email": sender_email,
            "hubspot_id": contact_id,
            "soggetto_email": subject[:80],
        }
        results.append(entry)
        log.info("[%s] %s (ID: %s)", status, sender_email, contact_id)

    return results, latest_id


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--auth", action="store_true", help="Run OAuth flow only")
    parser.add_argument("--once", action="store_true", help="Run single pass and exit")
    parser.add_argument("--interval", type=int, default=300, help="Poll interval in seconds (default: 300)")
    args = parser.parse_args()

    gmail_svc = get_gmail_service()
    if args.auth:
        log.info("OAuth flow complete. token.json saved.")
        return

    hs = get_hubspot_client()
    log.info("Starting Gmail → HubSpot sync (interval: %ds)", args.interval)

    while True:
        since_id = load_last_sync()
        log.info("Checking for new emails since message ID: %s", since_id or "beginning")
        results, latest_id = sync_once(gmail_svc, hs, since_id)

        if results:
            log.info("── Riepilogo run ──")
            for r in results:
                log.info("  [%s] %s → HubSpot ID: %s", r["stato"], r["email"], r["hubspot_id"])
            if latest_id:
                save_last_sync(latest_id)
        else:
            log.info("Nessuna nuova email trovata.")

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
