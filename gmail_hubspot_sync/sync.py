#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot.
"""

import os
import re
import json
import base64
import logging
from email.utils import parseaddr
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ───────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE = "https://api.hubapi.com"

STATE_FILE = os.getenv("STATE_FILE", ".sync_state.json")
SEARCH_DAYS = int(os.getenv("SEARCH_DAYS", "1"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Domains / prefixes treated as automated senders (skipped)
SKIP_PREFIXES = ("no-reply", "noreply", "nobody", "mailer-daemon", "postmaster",
                 "bounce", "bounces", "notifications", "unsubscribe")
SKIP_DOMAINS = ("spamgourmet.com",)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("gmail_hubspot_sync")

# ── Gmail helpers ─────────────────────────────────────────────────────────────

def gmail_service():
    creds: Optional[Credentials] = None
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
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_recent_messages(service, since_history_id: Optional[str] = None):
    """Return list of message metadata from inbox since last run."""
    query = f"in:inbox -from:me newer_than:{SEARCH_DAYS}d"
    messages = []
    page_token = None
    while True:
        params = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token
        result = service.users().messages().list(**params).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_message_headers(service, msg_id: str) -> dict:
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Date", "Subject"]
    ).execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"]["headers"]}
    return headers


# ── Contact parsing ───────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> Optional[dict]:
    """Extract name and email from a From header value."""
    name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None
    email = email.strip().lower()

    local, domain = email.split("@", 1)

    # Skip automated senders
    if local.startswith(SKIP_PREFIXES) or domain in SKIP_DOMAINS:
        return None

    # Parse name parts
    name = name.strip()
    parts = name.split() if name else []
    firstname = parts[0] if parts else local.split(".")[0].capitalize()
    lastname = " ".join(parts[1:]) if len(parts) > 1 else ""

    # Derive company from domain (strip common TLDs and subdomains)
    company = _domain_to_company(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def _domain_to_company(domain: str) -> str:
    """Best-effort company name from email domain."""
    # Strip leading www / mail / engage / c / etc.
    parts = domain.split(".")
    noise = {"www", "mail", "engage", "c", "e", "m", "info", "app", "get", "go"}
    parts = [p for p in parts if p not in noise]
    # Drop TLD(s) — keep first meaningful segment
    name = parts[0] if parts else domain
    return name.replace("-", " ").title()


# ── HubSpot helpers ───────────────────────────────────────────────────────────

HS_HEADERS = {
    "Authorization": f"Bearer {HUBSPOT_API_KEY}",
    "Content-Type": "application/json",
}


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns contact or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email}
        ]}],
        "properties": ["email", "firstname", "lastname", "company",
                       "lead_source", "hs_tag_ids"],
        "limit": 1,
    }
    r = requests.post(url, headers=HS_HEADERS, json=body, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(contact: dict) -> dict:
    """Create a new HubSpot contact and return it."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props = {
        "email": contact["email"],
        "firstname": contact["firstname"],
        "company": contact["company"],
        "lead_source": "Gmail",
    }
    if contact.get("lastname"):
        props["lastname"] = contact["lastname"]

    r = requests.post(url, headers=HS_HEADERS, json={"properties": props}, timeout=15)
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, contact: dict, existing: dict) -> dict:
    """Fill in missing fields on an existing HubSpot contact."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    existing_props = existing.get("properties", {})
    updates = {}

    if not existing_props.get("firstname") and contact["firstname"]:
        updates["firstname"] = contact["firstname"]
    if not existing_props.get("lastname") and contact.get("lastname"):
        updates["lastname"] = contact["lastname"]
    if not existing_props.get("company") and contact["company"]:
        updates["company"] = contact["company"]
    if not existing_props.get("lead_source"):
        updates["lead_source"] = "Gmail"

    if not updates:
        return existing  # nothing to patch

    r = requests.patch(url, headers=HS_HEADERS, json={"properties": updates}, timeout=15)
    r.raise_for_status()
    return r.json()


def hs_add_inbound_note(contact_id: str, subject: str, received_at: str):
    """Log an 'Inbound Gmail' engagement note on the contact."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    body = {
        "properties": {
            "hs_note_body": f"📧 Inbound Gmail\nSubject: {subject}\nReceived: {received_at}",
            "hs_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202}],
            }
        ],
    }
    r = requests.post(url, headers=HS_HEADERS, json=body, timeout=15)
    r.raise_for_status()


# ── State management ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        return json.loads(Path(STATE_FILE).read_text())
    return {"processed_ids": []}


def save_state(state: dict):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2))


# ── Main ──────────────────────────────────────────────────────────────────────

def process_messages(service, messages: list, processed_ids: set) -> list[dict]:
    results = []

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if msg_id in processed_ids:
            continue

        try:
            headers = get_message_headers(service, msg_id)
        except Exception as exc:
            log.warning("Failed to fetch message %s: %s", msg_id, exc)
            continue

        from_header = headers.get("from", "")
        subject = headers.get("subject", "(no subject)")
        date = headers.get("date", "")

        sender = parse_sender(from_header)
        if not sender:
            log.debug("Skipping automated/invalid sender: %s", from_header)
            processed_ids.add(msg_id)
            results.append({
                "status": "Ignorato",
                "email": from_header,
                "hubspot_id": None,
                "reason": "automated sender",
            })
            continue

        email = sender["email"]

        # ── HubSpot lookup ─────────────────────────────────────────────────
        try:
            existing = hs_find_contact(email)
        except Exception as exc:
            log.error("HubSpot lookup failed for %s: %s", email, exc)
            continue

        try:
            if existing:
                contact_id = existing["id"]
                hs_update_contact(contact_id, sender, existing)
                hs_add_inbound_note(contact_id, subject, date)
                status = "Aggiornato"
                log.info("Updated  %s  (id=%s)", email, contact_id)
            else:
                created = hs_create_contact(sender)
                contact_id = created["id"]
                hs_add_inbound_note(contact_id, subject, date)
                status = "Creato"
                log.info("Created  %s  (id=%s)", email, contact_id)
        except Exception as exc:
            log.error("HubSpot write failed for %s: %s", email, exc)
            continue

        processed_ids.add(msg_id)
        results.append({
            "status": status,
            "email": email,
            "hubspot_id": contact_id,
        })

    return results


def print_results(results: list[dict]):
    print("\n" + "─" * 60)
    print(f"{'STATUS':<12} {'EMAIL':<38} {'HUBSPOT ID'}")
    print("─" * 60)
    for r in results:
        hs_id = r["hubspot_id"] or "-"
        print(f"{r['status']:<12} {r['email']:<38} {hs_id}")
    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")
    print("─" * 60)
    print(f"Totale: {len(results)}  |  Creati: {created}  |  Aggiornati: {updated}  |  Ignorati: {ignored}\n")


def main():
    log.info("Starting Gmail → HubSpot sync")

    if not HUBSPOT_API_KEY:
        raise SystemExit("HUBSPOT_API_KEY env var is not set.")

    state = load_state()
    processed_ids = set(state.get("processed_ids", []))

    service = gmail_service()
    messages = fetch_recent_messages(service)
    log.info("Found %d messages in inbox (last %d day(s))", len(messages), SEARCH_DAYS)

    results = process_messages(service, messages, processed_ids)
    print_results(results)

    state["processed_ids"] = list(processed_ids)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    log.info("Sync complete. State saved to %s", STATE_FILE)


if __name__ == "__main__":
    main()
