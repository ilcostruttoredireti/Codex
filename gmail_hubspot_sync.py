"""
Gmail → HubSpot Contact Sync
Monitors inbound Gmail, extracts senders, and upserts them as HubSpot contacts.
Uses email as the unique key to prevent duplicates.

HubSpot field mapping:
  - hs_analytics_source = "EMAIL_MARKETING"  (source: Gmail inbound)
  - hs_lead_status      = "NEW"              (for newly created contacts)
"""

import re
import json
import time
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = "gmail_token.json"

HUBSPOT_API_KEY = "YOUR_HUBSPOT_PRIVATE_APP_TOKEN"
HUBSPOT_BASE = "https://api.hubapi.com"

# Patterns that identify automated / no-reply senders to skip
SKIP_PATTERNS = re.compile(
    r"(no-?reply|noreply|mailer-daemon|notification|newsletter|invoic|billing|"
    r"donotreply|bounce|automated|system@|nobody@|close_friend|facebookmail|"
    r"admanager|payments-noreply|googleworkspace|googleaistudio|serpapi|"
    r"academia-mail|cofidis|revolut\.com|discord\.com|aws\.com|adobe\.com|"
    r"skool\.com|moneya\.es|tiktok\.com|feedspot|academia)",
    re.IGNORECASE,
)

# How many days back to fetch
LOOKBACK_DAYS = 1


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #


@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    # Maps to HubSpot standard field hs_analytics_source (enum EMAIL_MARKETING)
    source: str = "EMAIL_MARKETING"


# --------------------------------------------------------------------------- #
# Gmail helpers
# --------------------------------------------------------------------------- #


def build_gmail_service(token_file: str = GMAIL_TOKEN_FILE):
    creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds)


def fetch_inbound_senders(service, lookback_days: int = LOOKBACK_DAYS) -> list[Contact]:
    """Return unique Contact objects for every real inbound sender."""
    query = f"in:inbox newer_than:{lookback_days}d -from:me -in:draft"
    results = service.users().messages().list(userId="me", q=query, maxResults=500).execute()
    messages = results.get("messages", [])

    seen: dict[str, Contact] = {}
    for msg_ref in messages:
        msg = service.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
        raw_from = headers.get("From", "")

        # Parse "Display Name <email@domain.com>" or bare "email@domain.com"
        match = re.search(r"<([^>]+)>", raw_from)
        email = match.group(1).strip().lower() if match else raw_from.strip().lower()
        display = raw_from.split("<")[0].strip().strip('"') if "<" in raw_from else ""

        if not email or SKIP_PATTERNS.search(email):
            continue
        if email in seen:
            continue

        domain = email.split("@")[-1]
        parts = display.split() if display else []
        firstname = parts[0] if parts else email.split("@")[0].replace(".", " ").title()
        lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
        company = _domain_to_company(domain)

        seen[email] = Contact(
            email=email,
            firstname=firstname,
            lastname=lastname,
            company=company,
            domain=domain,
        )


    return list(seen.values())


def _domain_to_company(domain: str) -> str:
    """Best-effort company name from email domain."""
    name = domain.split(".")[0].replace("-", " ").title()
    return name


# --------------------------------------------------------------------------- #
# HubSpot helpers
# --------------------------------------------------------------------------- #


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(client: httpx.Client, email: str) -> Optional[dict]:
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "leadsource", "hs_object_id"],
        "limit": 1,
    }
    r = client.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search", json=payload)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def create_contact(client: httpx.Client, contact: Contact) -> dict:
    props = {
        "email": contact.email,
        "firstname": contact.firstname,
        "lastname": contact.lastname,
        "company": contact.company,
        "hs_analytics_source": contact.source,
        "hs_lead_status": "NEW",
    }
    r = client.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts", json={"properties": props})
    r.raise_for_status()
    return r.json()


def update_contact(client: httpx.Client, contact_id: str, contact: Contact, existing: dict) -> dict:
    existing_props = existing.get("properties", {})
    updates = {}

    # Only fill in MISSING fields
    if not existing_props.get("firstname"):
        updates["firstname"] = contact.firstname
    if not existing_props.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not existing_props.get("company") and contact.company:
        updates["company"] = contact.company
    if not existing_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = contact.source

    if not updates:
        return existing  # nothing to change

    r = client.patch(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}", json={"properties": updates})
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Main sync loop
# --------------------------------------------------------------------------- #


def sync(lookback_days: int = LOOKBACK_DAYS) -> list[dict]:
    gmail = build_gmail_service()
    contacts = fetch_inbound_senders(gmail, lookback_days)
    log.info("Found %d unique inbound senders to process", len(contacts))

    report = []
    with httpx.Client(headers=_hs_headers(), timeout=30) as client:
        for contact in contacts:
            try:
                existing = find_contact_by_email(client, contact.email)
                if existing:
                    updated = update_contact(client, existing["id"], contact, existing)
                    # Check if we actually changed anything
                    changed = updated != existing
                    status = "Aggiornato" if changed else "Ignorato"
                    hs_id = existing["id"]
                else:
                    created = create_contact(client, contact)
                    status = "Creato"
                    hs_id = created["id"]

                row = {"status": status, "email": contact.email, "hubspot_id": hs_id}
                report.append(row)
                log.info("%s | %s | ID: %s", status, contact.email, hs_id)

                time.sleep(0.1)  # stay under HubSpot rate limits
            except Exception as exc:
                log.error("Error processing %s: %s", contact.email, exc)
                report.append({"status": "Errore", "email": contact.email, "hubspot_id": None})

    return report


if __name__ == "__main__":
    results = sync()
    print(json.dumps(results, indent=2, ensure_ascii=False))
