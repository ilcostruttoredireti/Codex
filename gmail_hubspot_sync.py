#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot automatically.
Avoids duplicates using email as unique key; creates or updates existing contacts.
"""

import os
import re
import time
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
HUBSPOT_BASE_URL = "https://api.hubapi.com"

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "60"))
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

# Senders whose local part contains these strings are skipped (automated mailers).
SKIP_LOCAL_PARTS = frozenset(
    {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "bounce", "notifications", "alert"}
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SenderContact:
    email: str
    first_name: str
    last_name: str
    company: str
    domain: str


@dataclass
class SyncResult:
    status: str           # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[str] = None
    reason: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.status}] {self.email}"]
        if self.hubspot_id:
            parts.append(f"→ HubSpot ID: {self.hubspot_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

def parse_sender(raw_sender: str) -> Optional[SenderContact]:
    """Parse a raw 'From' header value into a SenderContact. Returns None to skip."""
    name, email = parseaddr(raw_sender)
    email = email.lower().strip()
    if not email or "@" not in email:
        return None

    local, domain = email.rsplit("@", 1)

    # Skip automated senders.
    if any(skip in local for skip in SKIP_LOCAL_PARTS):
        return None

    company = _domain_to_company(domain)
    first_name, last_name = _split_name(name)

    # Fallback: derive name from email local part.
    if not first_name:
        parts = re.split(r"[._\-+]", local)
        first_name = parts[0].capitalize()
        last_name = parts[1].capitalize() if len(parts) > 1 else ""

    return SenderContact(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def _domain_to_company(domain: str) -> str:
    """Convert an email domain to a human-readable company name."""
    for prefix in ("www.", "mail.", "hello.", "news.", "info.", "support.", "newsletter.", "email."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    root = domain.split(".")[0]
    return root.replace("-", " ").replace("_", " ").title()


def _split_name(full_name: str) -> tuple[str, str]:
    full_name = full_name.strip()
    if not full_name:
        return "", ""
    parts = full_name.split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------

def build_gmail_service():
    """Authenticate and return a Gmail API service object."""
    creds: Optional[Credentials] = None
    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_threads(service, lookback_minutes: int) -> list[dict]:
    """Return inbox threads received in the last `lookback_minutes`."""
    after_dt = datetime.utcnow() - timedelta(minutes=lookback_minutes)
    after_str = after_dt.strftime("%Y/%m/%d")
    try:
        response = service.users().threads().list(
            userId="me",
            q=f"in:inbox after:{after_str}",
            maxResults=100,
        ).execute()
    except HttpError as exc:
        logging.error(f"Gmail API error listing threads: {exc}")
        return []

    threads = []
    for item in response.get("threads", []):
        try:
            detail = service.users().threads().get(
                userId="me",
                threadId=item["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError as exc:
            logging.warning(f"Could not fetch thread {item['id']}: {exc}")
            continue

        messages = detail.get("messages", [])
        if not messages:
            continue

        headers = {h["name"]: h["value"] for h in messages[0].get("payload", {}).get("headers", [])}
        threads.append({
            "id": item["id"],
            "sender": headers.get("From", ""),
            "subject": headers.get("Subject", "(no subject)"),
            "date": headers.get("Date", ""),
        })
    return threads


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _get(self, path: str, **kwargs) -> dict:
        r = requests.get(f"{HUBSPOT_BASE_URL}{path}", headers=self._headers, **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, payload: dict) -> dict:
        r = requests.post(f"{HUBSPOT_BASE_URL}{path}", json=payload, headers=self._headers)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, payload: dict) -> dict:
        r = requests.patch(f"{HUBSPOT_BASE_URL}{path}", json=payload, headers=self._headers)
        r.raise_for_status()
        return r.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
            "limit": 1,
        }
        data = self._post("/crm/v3/objects/contacts/search", payload)
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, contact: SenderContact) -> dict:
        props = {
            "email": contact.email,
            "firstname": contact.first_name,
            "lastname": contact.last_name,
            "company": contact.company,
            "hs_lead_source": CONTACT_SOURCE,
        }
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, contact: SenderContact, existing_props: dict) -> dict:
        updates: dict[str, str] = {}
        if not existing_props.get("firstname") and contact.first_name:
            updates["firstname"] = contact.first_name
        if not existing_props.get("lastname") and contact.last_name:
            updates["lastname"] = contact.last_name
        if not existing_props.get("company") and contact.company:
            updates["company"] = contact.company
        if not existing_props.get("hs_lead_source"):
            updates["hs_lead_source"] = CONTACT_SOURCE
        if not updates:
            return {"id": contact_id, "properties": existing_props}
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})

    def log_email_note(self, contact_id: str, subject: str, sender_email: str):
        """Create a HubSpot Note associated to a contact to record the received email."""
        body = (
            f"Email ricevuta da: {sender_email}\n"
            f"Oggetto: {subject}\n"
            f"Fonte: {CONTACT_SOURCE}\n"
            f"Tag: {CONTACT_TAG}"
        )
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/notes", payload)
        except requests.HTTPError as exc:
            logging.warning(f"Could not log note for contact {contact_id}: {exc}")


# ---------------------------------------------------------------------------
# Sync engine
# ---------------------------------------------------------------------------

class GmailHubSpotSync:
    def __init__(self):
        self.gmail = build_gmail_service()
        self.hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)
        self._seen_thread_ids: set[str] = set()
        self._seen_emails: set[str] = set()

    def process_thread(self, thread: dict) -> SyncResult:
        thread_id = thread["id"]
        sender_raw = thread["sender"]
        subject = thread["subject"]

        contact = parse_sender(sender_raw)
        if contact is None:
            return SyncResult(status="Ignorato", email=sender_raw, reason="mittente automatico")

        # De-duplicate within the same run (multiple threads from same address).
        if contact.email in self._seen_emails:
            return SyncResult(status="Ignorato", email=contact.email, reason="già processato in questo ciclo")
        self._seen_emails.add(contact.email)

        existing = self.hubspot.find_contact_by_email(contact.email)
        if existing:
            contact_id = existing["id"]
            self.hubspot.update_contact(contact_id, contact, existing.get("properties", {}))
            self.hubspot.log_email_note(contact_id, subject, contact.email)
            return SyncResult(status="Aggiornato", email=contact.email, hubspot_id=contact_id)
        else:
            created = self.hubspot.create_contact(contact)
            contact_id = created["id"]
            self.hubspot.log_email_note(contact_id, subject, contact.email)
            return SyncResult(status="Creato", email=contact.email, hubspot_id=contact_id)

    def run_once(self) -> list[SyncResult]:
        """Fetch new inbox threads and sync contacts. Returns results for this cycle."""
        self._seen_emails.clear()
        threads = fetch_inbox_threads(self.gmail, LOOKBACK_MINUTES)

        new_threads = [t for t in threads if t["id"] not in self._seen_thread_ids]
        self._seen_thread_ids.update(t["id"] for t in new_threads)

        results: list[SyncResult] = []
        for thread in new_threads:
            result = self.process_thread(thread)
            results.append(result)
            logging.info(str(result))
        return results

    def run_forever(self):
        """Poll Gmail continuously, syncing new contacts to HubSpot."""
        logging.info(
            f"Avvio sync Gmail→HubSpot | intervallo: {POLL_INTERVAL_SECONDS}s | "
            f"lookback: {LOOKBACK_MINUTES}min"
        )
        while True:
            try:
                results = self.run_once()
                stats = {s: sum(1 for r in results if r.status == s) for s in ("Creato", "Aggiornato", "Ignorato")}
                if results:
                    logging.info(
                        f"Ciclo completato: {stats['Creato']} creati, "
                        f"{stats['Aggiornato']} aggiornati, {stats['Ignorato']} ignorati"
                    )
            except Exception as exc:
                logging.error(f"Errore nel ciclo di sync: {exc}", exc_info=True)
            time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    GmailHubSpotSync().run_forever()
