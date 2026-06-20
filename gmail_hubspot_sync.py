#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.
Run as a scheduled job (e.g. cron every hour).

Requirements:
  pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests python-dotenv

Environment variables (.env):
  HUBSPOT_API_KEY      HubSpot Private App token
  GMAIL_CREDENTIALS    Path to OAuth2 credentials JSON (default: credentials.json)
  GMAIL_TOKEN          Path to token file (default: token.json)
  SYNC_LOOKBACK_HOURS  How many hours back to scan (default: 1)
"""

import os
import re
import json
import time
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_TOKEN = os.environ["HUBSPOT_API_KEY"]
LOOKBACK_HOURS = int(os.getenv("SYNC_LOOKBACK_HOURS", "1"))

SKIP_DOMAINS = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "libero.it",
                "virgilio.it", "tiscali.it", "alice.it", "icloud.com"}

# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds_path = os.getenv("GMAIL_CREDENTIALS", "credentials.json")
    token_path = os.getenv("GMAIL_TOKEN", "token.json")
    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_recent_senders(service, lookback_hours: int) -> list[dict]:
    """Return unique senders from inbox messages in the last N hours."""
    after_ts = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
    query = f"in:inbox after:{after_ts} -from:me"
    result = service.users().messages().list(
        userId="me", q=query, maxResults=100
    ).execute()

    messages = result.get("messages", [])
    seen_emails: set[str] = set()
    senders: list[dict] = []

    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")
        display_name, email_addr = parseaddr(raw_from)
        email_addr = email_addr.lower().strip()
        if not email_addr or "@" not in email_addr or email_addr in seen_emails:
            continue
        seen_emails.add(email_addr)
        domain = email_addr.split("@")[1]
        senders.append({
            "email": email_addr,
            "display_name": display_name.strip(),
            "domain": domain,
            "date": headers.get("Date", ""),
        })

    return senders


# ---------------------------------------------------------------------------
# Name parsing
# ---------------------------------------------------------------------------

def parse_name(display_name: str, domain: str) -> tuple[str, str, str]:
    """Returns (firstname, lastname, company)."""
    name = display_name.strip()
    parts = name.split()
    if len(parts) == 0:
        return "", "", _company_from_domain(domain)
    if len(parts) == 1:
        return parts[0], "", _company_from_domain(domain)
    # Heuristic: if name starts with known office keywords treat whole string as company
    office_keywords = {"ufficio", "uffici", "press", "comunicazione", "comunicati",
                       "stampa", "info", "media", "redazione", "news"}
    if parts[0].lower() in office_keywords:
        return name, "", name
    firstname = parts[0]
    lastname = " ".join(parts[1:])
    company = _company_from_domain(domain)
    return firstname, lastname, company


def _company_from_domain(domain: str) -> str:
    if domain in SKIP_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").title()


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(email: str) -> dict | None:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
        "limit": 1,
    }
    r = requests.post(url, json=body, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def create_contact(email: str, firstname: str, lastname: str, company: str) -> str:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props: dict[str, str] = {
        "email": email,
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    r = requests.post(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def update_contact(contact_id: str, existing: dict, firstname: str, lastname: str, company: str) -> bool:
    existing_props = existing.get("properties", {})
    updates: dict[str, str] = {}
    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company
    if not updates:
        return False
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = requests.patch(url, json={"properties": updates}, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    return True


def add_note(contact_id: str, email: str, date: str) -> None:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    body = {
        "properties": {
            "hs_note_body": f"Email inbound ricevuta da {email}\nData: {date}\nFonte: Gmail\nTag: Inbound Gmail",
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [
            {"to": {"id": contact_id}, "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]}
        ],
    }
    r = requests.post(url, json=body, headers=_hs_headers(), timeout=15)
    r.raise_for_status()


# ---------------------------------------------------------------------------
# Main sync loop
# ---------------------------------------------------------------------------

def sync_once() -> list[dict]:
    log.info("Starting Gmail → HubSpot sync (lookback %dh)", LOOKBACK_HOURS)
    service = get_gmail_service()
    senders = fetch_recent_senders(service, LOOKBACK_HOURS)
    log.info("Found %d unique senders", len(senders))

    results: list[dict] = []
    for sender in senders:
        email = sender["email"]
        display_name = sender["display_name"]
        domain = sender["domain"]
        date = sender["date"]

        firstname, lastname, company = parse_name(display_name, domain)
        existing = find_contact_by_email(email)

        try:
            if existing is None:
                contact_id = create_contact(email, firstname, lastname, company)
                add_note(contact_id, email, date)
                status = "CREATO"
                log.info("[CREATO] %s → ID %s", email, contact_id)
            else:
                contact_id = existing["id"]
                updated = update_contact(contact_id, existing, firstname, lastname, company)
                add_note(contact_id, email, date)
                status = "AGGIORNATO" if updated else "IGNORATO"
                log.info("[%s] %s → ID %s", status, email, contact_id)
        except requests.HTTPError as exc:
            status = f"ERRORE ({exc.response.status_code})"
            contact_id = ""
            log.error("Error processing %s: %s", email, exc)

        results.append({"stato": status, "email": email, "hubspot_id": contact_id})

    # Print summary table
    print("\n=== Sync Report ===")
    print(f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print("-" * 75)
    for r in results:
        print(f"{r['stato']:<12} {r['email']:<45} {r['hubspot_id']}")

    created = sum(1 for r in results if r["stato"] == "CREATO")
    updated = sum(1 for r in results if r["stato"] == "AGGIORNATO")
    ignored = sum(1 for r in results if r["stato"] == "IGNORATO")
    log.info("Done — Creati: %d | Aggiornati: %d | Ignorati: %d", created, updated, ignored)
    return results


if __name__ == "__main__":
    sync_once()
