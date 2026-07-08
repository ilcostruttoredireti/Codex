"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders and syncs contacts to HubSpot.
Requires: GMAIL_CREDENTIALS_JSON, HUBSPOT_API_KEY env vars.
"""

import os
import re
import json
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_API_BASE = "https://api.hubapi.com"
HUBSPOT_API_KEY = os.environ["HUBSPOT_API_KEY"]

# Patterns for automated/no-reply senders to skip
SKIP_PATTERNS = re.compile(
    r"(no.?reply|noreply|notify|donotreply|mailer.daemon|newsletter|"
    r"invitations|notifications|support|info@linkedin|friends@facebook|"
    r"bounce|postmaster|daemon|facebookmail\.com|googlemail\.com)",
    re.IGNORECASE,
)


# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = "token.json"
    creds_json = os.environ.get("GMAIL_CREDENTIALS_JSON")

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_json:
                raise RuntimeError("Set GMAIL_CREDENTIALS_JSON env var with OAuth client JSON")
            flow = InstalledAppFlow.from_client_config(json.loads(creds_json), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_recent_messages(service, hours_back: int = 24) -> list[dict]:
    since = int((datetime.now(timezone.utc) - timedelta(hours=hours_back)).timestamp())
    query = f"in:inbox after:{since} -from:me"
    result = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = result.get("messages", [])

    detailed = []
    for msg in messages:
        full = service.users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "Subject", "Date"]
        ).execute()
        headers = {h["name"]: h["value"] for h in full["payload"]["headers"]}
        detailed.append({
            "id": msg["id"],
            "from": headers.get("From", ""),
            "subject": headers.get("Subject", ""),
            "date": headers.get("Date", ""),
        })
    return detailed


# ── Contact extraction ─────────────────────────────────────────────────────────

def parse_sender(raw_from: str) -> dict | None:
    display_name, email = parseaddr(raw_from)
    if not email or "@" not in email:
        return None
    email = email.lower().strip()

    if SKIP_PATTERNS.search(email) or SKIP_PATTERNS.search(display_name):
        return None

    # Split name
    parts = display_name.strip().split() if display_name else []
    firstname = parts[0] if parts else email.split("@")[0].replace(".", " ").title()
    lastname = " ".join(parts[1:]) if len(parts) > 1 else None

    # Derive company from domain
    domain = email.split("@")[1]
    company_parts = domain.split(".")
    company = company_parts[0].replace("-", " ").title() if company_parts else None

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ── HubSpot ────────────────────────────────────────────────────────────────────

def hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def find_contact(email: str) -> dict | None:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    r = requests.post(url, headers=hs_headers(), json=payload, timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def create_contact(sender: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
    props = {
        "email": sender["email"],
        "firstname": sender["firstname"],
        "hs_analytics_source": "EMAIL_MARKETING",
    }
    if sender.get("lastname"):
        props["lastname"] = sender["lastname"]
    if sender.get("company"):
        props["company"] = sender["company"]

    r = requests.post(url, headers=hs_headers(), json={"properties": props}, timeout=10)
    r.raise_for_status()
    return r.json()


def update_contact(contact_id: str, sender: dict, existing: dict) -> dict:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/{contact_id}"
    existing_props = existing.get("properties", {})
    props = {}

    # Only fill missing fields
    if not existing_props.get("firstname") and sender.get("firstname"):
        props["firstname"] = sender["firstname"]
    if not existing_props.get("lastname") and sender.get("lastname"):
        props["lastname"] = sender["lastname"]
    if not existing_props.get("company") and sender.get("company"):
        props["company"] = sender["company"]
    if not existing_props.get("hs_analytics_source"):
        props["hs_analytics_source"] = "EMAIL_MARKETING"

    if not props:
        return existing  # nothing to update

    r = requests.patch(url, headers=hs_headers(), json={"properties": props}, timeout=10)
    r.raise_for_status()
    return r.json()


def add_note(contact_id: str, email_subject: str, sender_email: str) -> None:
    url = f"{HUBSPOT_API_BASE}/crm/v3/objects/notes"
    note_body = f"📧 Email ricevuta da {sender_email}\nOggetto: {email_subject}\nFonte: Gmail Inbound"
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    try:
        r = requests.post(url, headers=hs_headers(), json=payload, timeout=10)
        r.raise_for_status()
    except Exception as exc:
        log.warning("Note creation failed for %s: %s", contact_id, exc)


# ── Main sync ──────────────────────────────────────────────────────────────────

def sync(hours_back: int = 24) -> list[dict]:
    service = get_gmail_service()
    messages = fetch_recent_messages(service, hours_back)
    log.info("Fetched %d messages from Gmail", len(messages))

    seen_emails: set[str] = set()
    results = []

    for msg in messages:
        sender = parse_sender(msg["from"])
        if not sender:
            log.debug("Skipped automated sender: %s", msg["from"])
            continue

        email = sender["email"]
        if email in seen_emails:
            continue
        seen_emails.add(email)

        existing = find_contact(email)

        if existing:
            contact_id = existing["id"]
            updated = update_contact(contact_id, sender, existing)
            # Check if any fields were actually patched
            was_updated = updated.get("id") == contact_id and updated != existing
            status = "Aggiornato" if was_updated else "Ignorato"
        else:
            created = create_contact(sender)
            contact_id = created["id"]
            status = "Creato"

        add_note(contact_id, msg["subject"], email)

        row = {"stato": status, "email": email, "hubspot_id": contact_id}
        results.append(row)
        log.info("%s | %s | ID %s", status, email, contact_id)

    return results


if __name__ == "__main__":
    import argparse, sys

    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot")
    parser.add_argument("--hours", type=int, default=24, help="Look back N hours (default: 24)")
    args = parser.parse_args()

    rows = sync(hours_back=args.hours)
    print("\n=== Risultati sincronizzazione ===")
    print(f"{'Stato':<12} {'Email':<40} {'ID HubSpot'}")
    print("-" * 70)
    for r in rows:
        print(f"{r['stato']:<12} {r['email']:<40} {r['hubspot_id']}")
    print(f"\nTotale processati: {len(rows)}")
    sys.exit(0)
