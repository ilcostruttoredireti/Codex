#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail and syncs senders to HubSpot CRM automatically.

Usage:
    python gmail_hubspot_sync.py              # continuous polling (default 60s)
    python gmail_hubspot_sync.py --once       # single scan then exit
    python gmail_hubspot_sync.py --interval 30  # poll every 30s
"""

import argparse
import json
import logging
import os
import time
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
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.exceptions import ApiException

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = Path("token.json")
STATE_FILE = Path("processed_ids.json")

# Common free-email domains → skip company extraction
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
    "live.com", "live.it", "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me", "tiscali.it", "libero.it",
    "virgilio.it", "aol.com", "mail.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    """Authenticate via OAuth2 and return a Gmail API resource."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_ids(gmail, max_results: int = 100) -> list[str]:
    """Return recent INBOX message IDs (newest first)."""
    try:
        resp = gmail.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=max_results,
        ).execute()
        return [m["id"] for m in resp.get("messages", [])]
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return []


def fetch_message_headers(gmail, msg_id: str) -> dict:
    """Fetch only the headers we need for a given message ID."""
    try:
        msg = gmail.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        return {h["name"]: h["value"]
                for h in msg.get("payload", {}).get("headers", [])}
    except HttpError as exc:
        log.error("Gmail get error (%s): %s", msg_id, exc)
        return {}


# ── Parsing helpers ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> dict:
    """Parse a From header into structured contact fields."""
    display_name, email = parseaddr(from_header)
    email = email.strip().lower()
    display_name = display_name.strip()

    parts = display_name.split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""

    company = _company_from_domain(email)

    return {
        "email": email,
        "display_name": display_name,
        "first_name": first,
        "last_name": last,
        "company": company,
    }


def _company_from_domain(email: str) -> Optional[str]:
    """Derive a company name from the email domain, or None for free providers."""
    try:
        domain = email.split("@")[1].lower()
    except IndexError:
        return None
    if domain in FREE_DOMAINS:
        return None
    # acmecorp.com → Acmecorp  |  red-bull.co.uk → Red-bull
    name = domain.split(".")[0].replace("-", " ").title()
    return name or None


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return hubspot.Client.create(access_token=token)


def find_contact(hs: hubspot.Client, email: str):
    """Return the first HubSpot contact matching *email*, or None."""
    try:
        result = hs.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(filters=[
                        Filter(property_name="email", operator="EQ", value=email)
                    ])
                ],
                properties=["email", "firstname", "lastname", "company"],
                limit=1,
            )
        )
        return result.results[0] if result.total > 0 else None
    except ApiException as exc:
        log.error("HubSpot search error: %s", exc)
        return None


def create_contact(hs: hubspot.Client, sender: dict) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None."""
    props = {
        "email": sender["email"],
        "hs_lead_source": "Gmail",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        resp = hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return resp.id
    except ApiException as exc:
        log.error("HubSpot create error: %s", exc)
        return None


def update_contact(hs: hubspot.Client, contact_id: str, sender: dict, existing) -> bool:
    """Fill missing fields on an existing contact. Returns True on success."""
    ep = existing.properties or {}
    updates = {}

    if not ep.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not ep.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not ep.get("company") and sender["company"]:
        updates["company"] = sender["company"]

    if not updates:
        return True

    try:
        hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update error: %s", exc)
        return False


# ── State management ──────────────────────────────────────────────────────────

def load_processed() -> set:
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text()))
    return set()


def save_processed(ids: set) -> None:
    STATE_FILE.write_text(json.dumps(sorted(ids)))


# ── Core processing ───────────────────────────────────────────────────────────

def process_message(gmail, hs: hubspot.Client, msg_id: str, own_email: str) -> dict:
    """
    Process one Gmail message → sync sender to HubSpot.

    Returns a result dict with keys: status, email, hubspot_id.
    """
    headers = fetch_message_headers(gmail, msg_id)
    from_header = headers.get("From", "")

    if not from_header:
        return {"status": "Ignorato", "reason": "No From header", "email": "-", "hubspot_id": "-"}

    sender = parse_sender(from_header)

    if not sender["email"] or "@" not in sender["email"]:
        return {"status": "Ignorato", "reason": "Email non valida", "email": "-", "hubspot_id": "-"}

    if own_email and sender["email"] == own_email.lower():
        return {"status": "Ignorato", "reason": "Email propria", "email": sender["email"], "hubspot_id": "-"}

    existing = find_contact(hs, sender["email"])

    if existing:
        update_contact(hs, existing.id, sender, existing)
        return {"status": "Aggiornato", "email": sender["email"], "hubspot_id": existing.id}

    new_id = create_contact(hs, sender)
    if new_id:
        return {"status": "Creato", "email": sender["email"], "hubspot_id": new_id}

    return {"status": "Errore", "email": sender["email"], "hubspot_id": "-"}


def run_once(gmail, hs: hubspot.Client, processed: set, own_email: str) -> tuple[set, list]:
    """One scan cycle. Returns (updated processed set, list of result dicts)."""
    ids = fetch_inbox_ids(gmail)
    new_ids = [mid for mid in ids if mid not in processed]

    results = []
    for msg_id in new_ids:
        result = process_message(gmail, hs, msg_id, own_email)
        processed.add(msg_id)
        results.append(result)

        status = result["status"]
        email = result["email"]
        hs_id = result["hubspot_id"]
        log.info("%-12s | %-40s | HubSpot ID: %s", status, email, hs_id)

        time.sleep(0.3)  # gentle rate-limit buffer

    return processed, results


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Single scan, then exit")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")
    args = parser.parse_args()

    own_email = os.environ.get("OWN_EMAIL", "").strip().lower()
    gmail = get_gmail_service()
    hs = get_hubspot_client()
    processed = load_processed()

    log.info("Gmail → HubSpot sync avviato | modalità: %s | intervallo: %ds",
             "once" if args.once else "loop", args.interval)

    if args.once:
        processed, _ = run_once(gmail, hs, processed, own_email)
        save_processed(processed)
        return

    while True:
        try:
            processed, results = run_once(gmail, hs, processed, own_email)
            if results:
                save_processed(processed)
                creati = sum(1 for r in results if r["status"] == "Creato")
                aggiornati = sum(1 for r in results if r["status"] == "Aggiornato")
                ignorati = sum(1 for r in results if r["status"] == "Ignorato")
                log.info("Ciclo completato → Creati: %d | Aggiornati: %d | Ignorati: %d",
                         creati, aggiornati, ignorati)
        except Exception:
            log.exception("Errore nel ciclo di sync")

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
