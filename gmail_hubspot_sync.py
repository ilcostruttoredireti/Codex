#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors Gmail inbox, extracts sender info, and syncs contacts to HubSpot.
"""

import os
import re
import time
import json
import logging
import base64
import hashlib
from datetime import datetime, timezone
from email.utils import parseaddr
from dataclasses import dataclass, field, asdict
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE_URL = "https://api.hubapi.com"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
STATE_FILE = os.getenv("STATE_FILE", "sync_state.json")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Senders to always skip (automated/system addresses)
SKIP_SENDER_PATTERNS = [
    r"noreply",
    r"no-reply",
    r"mailer-daemon",
    r"postmaster",
    r"posta-certificata",
    r"legalmail\.it",
    r"notifications?@",
    r"do-not-reply",
    r"analytics-noreply",
    r"bounce",
]

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = field(init=False)

    def __post_init__(self):
        self.email = self.email.lower().strip()
        parts = self.email.split("@")
        self.domain = parts[1] if len(parts) == 2 else ""

    @property
    def full_name(self) -> str:
        return f"{self.firstname} {self.lastname}".strip()


@dataclass
class SyncResult:
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[str]
    reason: str = ""

    def display(self) -> str:
        icon = {"Creato": "🟢", "Aggiornato": "🔵", "Ignorato": "⚪"}.get(self.status, "❓")
        parts = [f"{icon} {self.status:10s} | {self.email:45s} | ID: {self.hubspot_id or 'N/A'}"]
        if self.reason:
            parts.append(f"  ({self.reason})")
        return " ".join(parts)


# ── State persistence ─────────────────────────────────────────────────────────

class SyncState:
    """Persists the last processed Gmail history ID to avoid reprocessing."""

    def __init__(self, path: str):
        self.path = path
        self._data: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def last_history_id(self) -> Optional[str]:
        return self._data.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str):
        self._data["last_history_id"] = value
        self.save()

    def processed_message_ids(self) -> set:
        return set(self._data.get("processed_message_ids", []))

    def mark_processed(self, message_id: str):
        ids = self.processed_message_ids()
        ids.add(message_id)
        # Keep only last 5000 to avoid unbounded growth
        if len(ids) > 5000:
            ids = set(list(ids)[-5000:])
        self._data["processed_message_ids"] = list(ids)
        self.save()


# ── Gmail client ──────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def is_system_sender(email: str) -> bool:
    email_lower = email.lower()
    return any(re.search(p, email_lower) for p in SKIP_SENDER_PATTERNS)


def parse_sender_name(raw_from: str) -> tuple[str, str]:
    """Return (firstname, lastname) parsed from a 'From:' header value."""
    display_name, _ = parseaddr(raw_from)
    display_name = display_name.strip().strip('"')
    if not display_name:
        return "", ""
    parts = display_name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def infer_company_from_domain(domain: str) -> str:
    """Best-effort company name from domain (skips generic providers)."""
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
               "libero.it", "icloud.com", "live.it", "tiscali.it"}
    if domain in generic:
        return ""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def fetch_new_messages(service, state: SyncState) -> list[dict]:
    """Return list of raw message dicts for unprocessed inbound emails."""
    processed = state.processed_message_ids()
    messages = []

    query = "in:inbox -from:me"
    result = service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    raw_messages = result.get("messages", [])

    for m in raw_messages:
        msg_id = m["id"]
        if msg_id in processed:
            continue
        msg = service.users().messages().get(userId="me", id=msg_id, format="metadata",
                                              metadataHeaders=["From", "To", "Subject", "Date"]).execute()
        messages.append(msg)

    return messages


def extract_sender(message: dict) -> Optional[SenderContact]:
    """Parse a Gmail message and return a SenderContact, or None if should be skipped."""
    headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    _, email_addr = parseaddr(raw_from)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None
    if is_system_sender(email_addr):
        return None

    firstname, lastname = parse_sender_name(raw_from)
    domain = email_addr.split("@")[1]
    company = infer_company_from_domain(domain)

    return SenderContact(
        email=email_addr,
        firstname=firstname,
        lastname=lastname,
        company=company,
    )


# ── HubSpot client ────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, **kwargs) -> dict:
        r = self.session.get(f"{HUBSPOT_BASE_URL}{path}", **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        r = self.session.post(f"{HUBSPOT_BASE_URL}{path}", json=payload)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict) -> dict:
        r = self.session.patch(f"{HUBSPOT_BASE_URL}{path}", json=payload)
        r.raise_for_status()
        return r.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        payload = {
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
            "limit": 1,
        }
        data = self._post("/crm/v3/objects/contacts/search", payload)
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, contact: SenderContact) -> dict:
        properties = {
            "email": contact.email,
            "hs_lead_status": "NEW",
            "hs_analytics_source": "OTHER",
        }
        if contact.firstname:
            properties["firstname"] = contact.firstname
        if contact.lastname:
            properties["lastname"] = contact.lastname
        if contact.company:
            properties["company"] = contact.company
        return self._post("/crm/v3/objects/contacts", {"properties": properties})

    def update_contact(self, contact_id: str, updates: dict) -> dict:
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})

    def add_note(self, contact_id: str, body: str):
        """Associate an engagement note with the contact."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        note_payload = {
            "engagement": {"active": True, "type": "NOTE", "timestamp": now_ms},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {"body": body},
        }
        try:
            self._post("/engagements/v1/engagements", note_payload)
        except Exception as e:
            log.warning("Could not create engagement note: %s", e)


# ── Sync logic ────────────────────────────────────────────────────────────────

def compute_updates(existing: dict, contact: SenderContact) -> dict:
    """Return only the properties that are missing/empty in HubSpot."""
    props = existing.get("properties", {})
    updates = {}

    if not props.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not props.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not props.get("company") and contact.company:
        updates["company"] = contact.company
    if not props.get("hs_lead_status"):
        updates["hs_lead_status"] = "NEW"

    return updates


def sync_contact(hs: HubSpotClient, contact: SenderContact, subject: str, message_id: str) -> SyncResult:
    existing = hs.find_contact_by_email(contact.email)

    if existing:
        contact_id = existing["id"]
        updates = compute_updates(existing, contact)
        if updates:
            hs.update_contact(contact_id, updates)
            log.info("Updated contact %s (%s): %s", contact.email, contact_id, updates)
            status = "Aggiornato"
        else:
            status = "Ignorato"

        hs.add_note(contact_id,
                    f"Email ricevuta via Gmail (Inbound)\nOggetto: {subject}\nMessage-ID: {message_id}\n"
                    f"Fonte: Gmail | Tag: Inbound Gmail")
        return SyncResult(status=status, email=contact.email, hubspot_id=contact_id,
                          reason=f"aggiornati: {list(updates.keys())}" if updates else "nessun campo mancante")

    # New contact
    created = hs.create_contact(contact)
    contact_id = created["id"]
    hs.add_note(contact_id,
                f"Contatto creato automaticamente da Gmail Sync.\nOggetto prima email: {subject}\n"
                f"Fonte: Gmail | Tag: Inbound Gmail")
    log.info("Created contact %s (ID: %s)", contact.email, contact_id)
    return SyncResult(status="Creato", email=contact.email, hubspot_id=contact_id)


# ── Main loop ─────────────────────────────────────────────────────────────────

def run():
    if not HUBSPOT_API_KEY:
        raise EnvironmentError("HUBSPOT_API_KEY environment variable not set")

    state = SyncState(STATE_FILE)
    hs = HubSpotClient(HUBSPOT_API_KEY)
    gmail = get_gmail_service()

    log.info("Gmail → HubSpot sync started. Poll interval: %ds", POLL_INTERVAL_SECONDS)

    while True:
        try:
            log.info("Polling Gmail inbox…")
            messages = fetch_new_messages(gmail, state)
            log.info("Found %d new message(s) to process", len(messages))

            results: list[SyncResult] = []
            seen_in_batch: set[str] = set()

            for msg in messages:
                msg_id = msg["id"]
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                subject = headers.get("Subject", "(no subject)")

                contact = extract_sender(msg)
                if contact is None:
                    state.mark_processed(msg_id)
                    continue

                # Deduplicate within the same polling batch
                if contact.email in seen_in_batch:
                    state.mark_processed(msg_id)
                    continue
                seen_in_batch.add(contact.email)

                try:
                    result = sync_contact(hs, contact, subject, msg_id)
                    results.append(result)
                    log.info(result.display())
                except Exception as e:
                    log.error("Failed to sync %s: %s", contact.email, e)
                    results.append(SyncResult(status="Ignorato", email=contact.email,
                                              hubspot_id=None, reason=str(e)))

                state.mark_processed(msg_id)

            if results:
                print("\n" + "─" * 90)
                print(f"  Sync completato: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print("─" * 90)
                for r in results:
                    print(r.display())
                created = sum(1 for r in results if r.status == "Creato")
                updated = sum(1 for r in results if r.status == "Aggiornato")
                skipped = sum(1 for r in results if r.status == "Ignorato")
                print(f"\n  Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {skipped}")
                print("─" * 90 + "\n")

        except Exception as e:
            log.error("Polling error: %s", e, exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
