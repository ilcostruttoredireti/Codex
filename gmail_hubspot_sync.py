"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and syncs senders as HubSpot contacts.
Avoids duplicates by using email as the unique key.
"""

import os
import re
import json
import time
import base64
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
HUBSPOT_BASE = "https://api.hubapi.com"

LOOKBACK_HOURS = int(os.getenv("LOOKBACK_HOURS", "24"))

# Senders to always skip
SKIP_PATTERNS = re.compile(
    r"(noreply|no-reply|donotreply|mailer-daemon|postmaster|"
    r"notification|notifications|bounce|bounces|auto-reply|"
    r"confirm|confirma|conferma|sc-noreply|googledev-noreply|"
    r"notify-noreply|sellersupport|premium@academia)@",
    re.IGNORECASE,
)

# Large platform domains whose emails aren't real contacts
SKIP_DOMAINS = {
    "google.com", "youtube.com", "amazon.com", "amazon.it", "amazon.co.uk",
    "ebay.com", "ebay.it", "revolut.com", "paypal.com", "stripe.com",
    "facebook.com", "instagram.com", "twitter.com", "linkedin.com",
    "tiktok.com", "shop.tiktok.com", "skool.com", "patreon.com",
    "hubspot.com", "manychat.com",
}


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
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


def fetch_inbox_senders(service, lookback_hours: int) -> list[dict]:
    """Return unique senders from inbox messages received in the last N hours."""
    since = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    # Gmail date filter uses integer unix timestamp
    after_ts = int(since.timestamp())
    query = f"in:inbox -from:me after:{after_ts}"

    seen_emails: set[str] = set()
    senders: list[dict] = []
    page_token = None

    while True:
        params = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            params["pageToken"] = page_token

        resp = service.users().messages().list(**params).execute()
        messages = resp.get("messages", [])

        for msg_stub in messages:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=msg_stub["id"], format="metadata",
                     metadataHeaders=["From", "Date"])
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            raw_from = headers.get("From", "")
            name, email = parseaddr(raw_from)
            email = email.lower().strip()

            if not email or email in seen_emails:
                continue
            if should_skip(email):
                continue

            seen_emails.add(email)
            senders.append({"email": email, "name": name.strip()})

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return senders


def should_skip(email: str) -> bool:
    if SKIP_PATTERNS.search(email):
        return True
    domain = email.split("@")[-1].lower() if "@" in email else ""
    return domain in SKIP_DOMAINS


# ── Sender info extraction ─────────────────────────────────────────────────────

def parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → (firstname, lastname). Handles single names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return (parts[0] if parts else ""), ""


def company_from_domain(email: str) -> str:
    """Best-effort company name from email domain."""
    domain = email.split("@")[-1].lower()
    # Strip common TLDs and formatting
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def find_contact_by_email(email: str) -> dict | None:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    resp = requests.post(url, headers=hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(props: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, props: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def add_note(contact_id: str, body: str) -> None:
    """Create an engagement Note associated with the contact."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    note = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
                ],
            }
        ],
    }
    resp = requests.post(url, headers=hs_headers(), json=note, timeout=15)
    resp.raise_for_status()


# ── Core sync logic ────────────────────────────────────────────────────────────

def sync_sender(sender: dict) -> dict:
    """
    Process one Gmail sender.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "hubspot_id": ...}
    """
    email = sender["email"]
    display = sender.get("name", "")
    firstname, lastname = parse_name(display) if display else ("", "")
    company = company_from_domain(email)

    existing = find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict = {}

        # Fill in missing fields only
        if not props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not props.get("company"):
            updates["company"] = company
        if not props.get("hs_analytics_source"):
            updates["hs_analytics_source"] = "EMAIL"

        if updates:
            update_contact(contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"
    else:
        props = {
            "email": email,
            "company": company,
            "hs_analytics_source": "EMAIL",
            "hs_analytics_source_data_1": "Gmail Inbound",
        }
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname

        result = create_contact(props)
        contact_id = result["id"]

        # Log the inbound email as a note
        note_body = (
            f"[Inbound Gmail] Email received from {email}. "
            f"Contact auto-created by Gmail sync routine."
        )
        try:
            add_note(contact_id, note_body)
        except Exception as exc:
            log.warning("Could not add note to %s: %s", contact_id, exc)

        status = "Creato"

    return {"status": status, "email": email, "hubspot_id": contact_id}


# ── Entry point ────────────────────────────────────────────────────────────────

def run_sync() -> list[dict]:
    if not HUBSPOT_TOKEN:
        raise EnvironmentError("HUBSPOT_TOKEN environment variable is not set.")

    log.info("Starting Gmail → HubSpot sync (lookback=%dh)", LOOKBACK_HOURS)
    service = get_gmail_service()
    senders = fetch_inbox_senders(service, LOOKBACK_HOURS)
    log.info("Found %d unique senders to process.", len(senders))

    results = []
    for sender in senders:
        try:
            result = sync_sender(sender)
            results.append(result)
            log.info("[%s] %s → HubSpot ID %s", result["status"], result["email"], result["hubspot_id"])
            time.sleep(0.1)  # gentle rate limiting
        except Exception as exc:
            log.error("Error processing %s: %s", sender["email"], exc)
            results.append({"status": "Errore", "email": sender["email"], "hubspot_id": None})

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    ignored = sum(1 for r in results if r["status"] == "Ignorato")

    log.info("Sync complete — Creati: %d, Aggiornati: %d, Ignorati: %d", created, updated, ignored)
    return results


if __name__ == "__main__":
    output = run_sync()
    print(json.dumps(output, indent=2, ensure_ascii=False))
