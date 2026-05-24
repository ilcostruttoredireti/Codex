#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and automatically syncs sender contacts to HubSpot.
"""

import os
import re
import time
import logging
import json
from datetime import datetime, timezone
from typing import Optional
from email.utils import parseaddr

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

# ─── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("sync.log"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─── Configuration ─────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.pickle")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Domains to skip (system, internal)
SKIP_DOMAINS = {"accounts.google.com", "googlemail.com"}
SKIP_EMAILS_PATTERN = re.compile(
    r"^(no-reply|noreply|mailer-daemon|postmaster)@", re.IGNORECASE
)

# Personal/generic email domains (no company extraction)
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "virgilio.it", "libero.it", "tin.it", "alice.it", "tiscali.it"
}

STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")


# ─── State Management ───────────────────────────────────────────────────────
def load_state() -> dict:
    """Load sync state from disk."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_history_id": None, "processed_thread_ids": []}


def save_state(state: dict):
    """Persist sync state to disk."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ─── Gmail Auth ─────────────────────────────────────────────────────────────
def get_gmail_service():
    """Authenticate and return Gmail API service."""
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        with open(GMAIL_TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


# ─── Contact Extraction ─────────────────────────────────────────────────────
def extract_contact_from_message(message: dict) -> Optional[dict]:
    """
    Extract contact info from a Gmail message object.
    Handles both direct and forwarded emails.
    Returns dict with email, firstname, lastname, company or None.
    """
    headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}

    from_header = headers.get("From", "")
    subject = headers.get("Subject", "")

    # Parse display name and email from From: header
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    # Check for forwarded email – look in body snippet for "Da:" / "From:" patterns
    body_snippet = message.get("snippet", "")
    forwarded_contact = _extract_forwarded_contact(body_snippet)
    if forwarded_contact:
        email_addr = forwarded_contact["email"]
        display_name = forwarded_contact.get("name", "")

    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]

    # Skip system/internal domains
    if domain in SKIP_DOMAINS:
        return None
    if SKIP_EMAILS_PATTERN.match(email_addr):
        return None

    # Parse name
    firstname, lastname = _parse_name(display_name, email_addr)

    # Extract company from domain
    company = _company_from_domain(domain)

    return {
        "email": email_addr,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
        "display_name": display_name,
        "subject": subject,
    }


def _extract_forwarded_contact(snippet: str) -> Optional[dict]:
    """
    Parse 'Da "Name" email@domain.com' or 'From: "Name" <email>' patterns
    from email body snippets (forwarded messages).
    """
    # Italian forward pattern: Da "Nome Cognome" email@domain.com
    it_pattern = re.search(
        r'Da\s+"([^"]+)"\s+([\w._%+-]+@[\w.-]+\.[a-zA-Z]{2,})',
        snippet, re.IGNORECASE
    )
    if it_pattern:
        return {"name": it_pattern.group(1), "email": it_pattern.group(2).lower()}

    # No-name Italian pattern: Da email@domain.com
    it_email_only = re.search(
        r'Da\s+([\w._%+-]+@[\w.-]+\.[a-zA-Z]{2,})',
        snippet, re.IGNORECASE
    )
    if it_email_only:
        return {"name": "", "email": it_email_only.group(1).lower()}

    # English forward pattern: From: "Name" <email>
    en_pattern = re.search(
        r'From:\s*"?([^"<\n]+)"?\s*<?([\w._%+-]+@[\w.-]+\.[a-zA-Z]{2,})>?',
        snippet, re.IGNORECASE
    )
    if en_pattern:
        return {"name": en_pattern.group(1).strip(), "email": en_pattern.group(2).lower()}

    return None


def _parse_name(display_name: str, email_addr: str) -> tuple[str, str]:
    """Extract firstname and lastname from display name or email."""
    if display_name:
        parts = display_name.strip().split()
        if len(parts) >= 2:
            return parts[0], " ".join(parts[1:])
        elif len(parts) == 1:
            return parts[0], ""

    # Fallback: try to extract name from email local part
    local = email_addr.split("@")[0]
    # Handle patterns like firstname.lastname or firstname_lastname
    clean = re.split(r"[._-]", local)
    if len(clean) >= 2 and all(p.isalpha() for p in clean[:2]):
        return clean[0].capitalize(), clean[1].capitalize()

    return "", ""


def _company_from_domain(domain: str) -> str:
    """Infer company name from email domain, skip personal domains."""
    if domain in PERSONAL_DOMAINS:
        return ""

    # Remove common TLD and subdomains, capitalize
    parts = domain.split(".")
    # Remove www, mail, etc.
    meaningful = [p for p in parts if p not in ("www", "mail", "smtp", "m")]

    if not meaningful:
        return ""

    # Take main domain name (before TLD)
    company_raw = meaningful[-2] if len(meaningful) >= 2 else meaningful[0]

    # Clean up and capitalize
    company = re.sub(r"[-_]", " ", company_raw).title()
    return company


# ─── HubSpot API ────────────────────────────────────────────────────────────
class HubSpotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, params: dict = None) -> dict:
        resp = self.session.get(f"{HUBSPOT_BASE_URL}{path}", params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict) -> dict:
        resp = self.session.post(f"{HUBSPOT_BASE_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, body: dict) -> dict:
        resp = self.session.patch(f"{HUBSPOT_BASE_URL}{path}", json=body)
        resp.raise_for_status()
        return resp.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Search HubSpot for contact by email address."""
        try:
            body = {
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email
                    }]
                }],
                "properties": ["email", "firstname", "lastname", "company",
                               "hs_lead_status", "hs_analytics_source", "notes_last_contacted"],
                "limit": 1
            }
            result = self._post("/crm/v3/objects/contacts/search", body)
            results = result.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as e:
            log.error(f"HubSpot search error for {email}: {e}")
            return None

    def create_contact(self, contact: dict) -> dict:
        """Create a new HubSpot contact."""
        properties = self._build_properties(contact, is_new=True)
        body = {"properties": properties}
        return self._post("/crm/v3/objects/contacts", body)

    def update_contact(self, contact_id: str, contact: dict, existing: dict) -> dict:
        """Update existing HubSpot contact with missing fields."""
        existing_props = existing.get("properties", {})
        properties = {}

        # Only update fields that are empty/missing in HubSpot
        update_map = {
            "firstname": contact.get("firstname", ""),
            "lastname": contact.get("lastname", ""),
            "company": contact.get("company", ""),
        }
        for field, value in update_map.items():
            if value and not existing_props.get(field):
                properties[field] = value

        # Always set source if not already set to Gmail
        if not existing_props.get("hs_analytics_source"):
            properties["hs_analytics_source"] = CONTACT_SOURCE

        if not properties:
            return {"id": contact_id, "status": "no_changes"}

        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties})

    def _build_properties(self, contact: dict, is_new: bool = False) -> dict:
        """Build HubSpot properties dict from contact data."""
        props = {
            "email": contact["email"],
            "hs_analytics_source": CONTACT_SOURCE,
        }
        if contact.get("firstname"):
            props["firstname"] = contact["firstname"]
        if contact.get("lastname"):
            props["lastname"] = contact["lastname"]
        if contact.get("company"):
            props["company"] = contact["company"]
        if is_new:
            props["lifecyclestage"] = "lead"
        return props

    def log_email_activity(self, contact_id: str, subject: str, sender_email: str):
        """Log an email received activity on the contact timeline."""
        try:
            body = {
                "engagement": {"type": "EMAIL", "active": True},
                "associations": {"contactIds": [int(contact_id)]},
                "metadata": {
                    "from": {"email": sender_email},
                    "subject": subject,
                    "body": f"Email ricevuta via Gmail (Inbound)",
                }
            }
            self._post("/engagements/v1/engagements", body)
        except Exception as e:
            log.warning(f"Could not log activity for contact {contact_id}: {e}")


# ─── Core Sync Logic ─────────────────────────────────────────────────────────
def process_contact(hs: HubSpotClient, contact_data: dict) -> dict:
    """
    Core logic: check if contact exists in HubSpot, create or update.
    Returns result dict with status, email, and HubSpot contact ID.
    """
    email = contact_data["email"]
    existing = hs.find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        update_result = hs.update_contact(contact_id, contact_data, existing)

        if update_result.get("status") == "no_changes":
            status = "IGNORATO"
        else:
            status = "AGGIORNATO"

        # Log email activity (optional)
        if contact_data.get("subject"):
            hs.log_email_activity(contact_id, contact_data["subject"], email)

        return {"status": status, "email": email, "hubspot_id": contact_id}

    else:
        new_contact = hs.create_contact(contact_data)
        contact_id = new_contact["id"]

        # Log activity
        if contact_data.get("subject"):
            hs.log_email_activity(contact_id, contact_data["subject"], email)

        return {"status": "CREATO", "email": email, "hubspot_id": contact_id}


def fetch_new_threads(gmail_service, state: dict) -> list:
    """Fetch threads newer than last processed, return list of message dicts."""
    query = "in:inbox -from:me -category:updates -category:promotions is:unread"

    try:
        results = gmail_service.users().threads().list(
            userId="me",
            q=query,
            maxResults=50
        ).execute()
    except Exception as e:
        log.error(f"Gmail fetch error: {e}")
        return []

    threads = results.get("threads", [])
    processed_ids = set(state.get("processed_thread_ids", []))
    new_threads = []

    for thread in threads:
        thread_id = thread["id"]
        if thread_id not in processed_ids:
            try:
                thread_detail = gmail_service.users().threads().get(
                    userId="me", threadId=thread_id, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]
                ).execute()
                messages = thread_detail.get("messages", [])
                if messages:
                    new_threads.append((thread_id, messages[0]))
            except Exception as e:
                log.warning(f"Could not fetch thread {thread_id}: {e}")

    return new_threads


def run_sync_cycle(gmail_service, hs: HubSpotClient, state: dict) -> list:
    """Run one full sync cycle. Returns list of result dicts."""
    log.info("🔄 Starting sync cycle...")
    new_threads = fetch_new_threads(gmail_service, state)

    if not new_threads:
        log.info("✅ No new threads to process.")
        return []

    log.info(f"📬 Found {len(new_threads)} new thread(s) to process.")
    results = []
    processed_ids = set(state.get("processed_thread_ids", []))

    for thread_id, message in new_threads:
        contact_data = extract_contact_from_message(message)

        if not contact_data:
            log.info(f"  ⏭️  Skipped thread {thread_id} (no extractable contact)")
            processed_ids.add(thread_id)
            continue

        email = contact_data["email"]
        log.info(f"  📧 Processing: {email} ({contact_data.get('display_name', '')})")

        try:
            result = process_contact(hs, contact_data)
            results.append(result)

            icon = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "⏭️"}.get(result["status"], "❓")
            log.info(f"  {icon} {result['status']:10} | {email:40} | ID: {result['hubspot_id']}")
        except Exception as e:
            log.error(f"  ❌ Error processing {email}: {e}")
            results.append({"status": "ERRORE", "email": email, "hubspot_id": None, "error": str(e)})

        processed_ids.add(thread_id)

    # Keep only last 1000 processed IDs to avoid unbounded growth
    state["processed_thread_ids"] = list(processed_ids)[-1000:]
    state["last_sync"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return results


def print_summary(results: list):
    """Print a formatted summary table."""
    if not results:
        return

    print("\n" + "═" * 70)
    print(f"{'STATO':<12} {'EMAIL':<40} {'ID HUBSPOT'}")
    print("─" * 70)
    for r in results:
        icon = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "⏭️", "ERRORE": "❌"}.get(r["status"], "")
        print(f"{icon} {r['status']:<10} {r['email']:<40} {r.get('hubspot_id', 'N/A')}")
    print("═" * 70)

    created = sum(1 for r in results if r["status"] == "CREATO")
    updated = sum(1 for r in results if r["status"] == "AGGIORNATO")
    skipped = sum(1 for r in results if r["status"] == "IGNORATO")
    errors = sum(1 for r in results if r["status"] == "ERRORE")
    print(f"✅ Creati: {created} | 🔄 Aggiornati: {updated} | ⏭️ Ignorati: {skipped} | ❌ Errori: {errors}")
    print()


# ─── Entry Point ────────────────────────────────────────────────────────────
def main():
    log.info("🚀 Gmail → HubSpot Contact Sync started")

    if not HUBSPOT_API_KEY:
        log.error("❌ HUBSPOT_API_KEY environment variable not set!")
        return

    gmail_service = get_gmail_service()
    hs = HubSpotClient(HUBSPOT_API_KEY)
    state = load_state()

    log.info(f"⏰ Poll interval: {POLL_INTERVAL_SECONDS}s ({POLL_INTERVAL_SECONDS//60}min)")

    while True:
        try:
            results = run_sync_cycle(gmail_service, hs, state)
            print_summary(results)
        except KeyboardInterrupt:
            log.info("👋 Stopped by user.")
            break
        except Exception as e:
            log.error(f"❌ Unexpected error in sync cycle: {e}")

        log.info(f"💤 Sleeping {POLL_INTERVAL_SECONDS}s until next cycle...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
