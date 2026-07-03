#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail, extracts senders, creates/updates HubSpot contacts.
"""

import os
import re
import json
import base64
import logging
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "gmail_credentials.json")

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE = "https://api.hubapi.com"

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"
SEARCH_DAYS = int(os.getenv("SYNC_DAYS", "1"))          # look-back window
MAX_THREADS = int(os.getenv("SYNC_MAX_THREADS", "100")) # threads per run

# Patterns that identify automated/transactional senders – skip these
SKIP_PATTERNS = re.compile(
    r"(no.?reply|noreply|donotreply|notifications?|postmaster|mailer.daemon"
    r"|invoicing|billing|conferma|conferme|spedizion|ordine|payments?"
    r"|bounce|alert|automatic|daemon|system|admin@)",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source_label: str = CONTACT_SOURCE
    tag: str = CONTACT_TAG

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1] if "@" in self.email else ""

    def company_from_domain(self) -> str:
        """Derive a human-readable company name from the email domain."""
        dom = self.domain
        # strip common mail-relay subdomains
        parts = dom.split(".")
        for i, p in enumerate(parts):
            if p not in ("mail", "news", "email", "info", "updates", "get", "notifications"):
                name = ".".join(parts[i:])
                break
        else:
            name = dom
        # strip TLD for display
        name = re.sub(r"\.(com|it|eu|io|ai|net|org|co)$", "", name, flags=re.I)
        return name.replace("-", " ").title()


@dataclass
class SyncResult:
    email: str
    status: str          # "Creato" | "Aggiornato" | "Ignorato"
    contact_id: str = ""
    notes: str = ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────

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
        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_senders(service, days: int = 1) -> list[SenderContact]:
    """Return unique SenderContact objects from inbox messages in the last N days."""
    query = f"in:inbox -in:sent -in:draft newer_than:{days}d"
    seen_emails: set[str] = set()
    contacts: list[SenderContact] = []
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
                     metadataHeaders=["From", "Subject"])
                .execute()
            )
            from_header = next(
                (h["value"] for h in msg.get("payload", {}).get("headers", [])
                 if h["name"] == "From"),
                "",
            )
            if not from_header:
                continue

            name, email_addr = parseaddr(from_header)
            email_addr = email_addr.lower().strip()

            if not email_addr or email_addr in seen_emails:
                continue
            if SKIP_PATTERNS.search(email_addr):
                log.debug("Skipping automated sender: %s", email_addr)
                continue

            seen_emails.add(email_addr)
            contact = _parse_sender(name, email_addr)
            contacts.append(contact)

        page_token = resp.get("nextPageToken")
        if not page_token or len(contacts) >= MAX_THREADS:
            break

    return contacts


def _parse_sender(display_name: str, email: str) -> SenderContact:
    """Build a SenderContact from the From: header display name and address."""
    c = SenderContact(email=email)

    # Try to extract first/last from display name
    clean = re.sub(r"[\"'<>]", "", display_name).strip()
    if clean:
        parts = clean.split()
        if len(parts) == 1:
            c.firstname = parts[0]
        elif len(parts) >= 2:
            c.firstname = parts[0]
            c.lastname = " ".join(parts[1:])

    # Company from display name or domain
    c.company = c.company_from_domain()

    return c


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def search_contact_by_email(email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company",
                       "hs_analytics_source", "hs_contact_source_label"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_hs_headers(), json=body, timeout=10)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(c: SenderContact) -> dict:
    """Create a new HubSpot contact. Returns the created object."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props = {
        "email": c.email,
        "hs_analytics_source": c.source_label,
    }
    if c.firstname:
        props["firstname"] = c.firstname
    if c.lastname:
        props["lastname"] = c.lastname
    if c.company:
        props["company"] = c.company

    resp = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, updates: dict) -> dict:
    """Patch missing fields on an existing HubSpot contact."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_hs_headers(), json={"properties": updates}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def sync_contact(c: SenderContact) -> SyncResult:
    """Core logic: check for duplicates, create or update, return result."""
    existing = search_contact_by_email(c.email)

    if existing is None:
        created = create_contact(c)
        return SyncResult(
            email=c.email,
            status="Creato",
            contact_id=created["id"],
        )

    # Contact exists – patch only blank fields
    existing_props = existing.get("properties", {})
    contact_id = existing["id"]
    updates: dict = {}

    if not existing_props.get("firstname") and c.firstname:
        updates["firstname"] = c.firstname
    if not existing_props.get("lastname") and c.lastname:
        updates["lastname"] = c.lastname
    if not existing_props.get("company") and c.company:
        updates["company"] = c.company
    if not existing_props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = c.source_label

    if updates:
        update_contact(contact_id, updates)
        return SyncResult(
            email=c.email,
            status="Aggiornato",
            contact_id=contact_id,
            notes=f"Aggiornati: {list(updates.keys())}",
        )

    return SyncResult(
        email=c.email,
        status="Ignorato",
        contact_id=contact_id,
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def run_sync():
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY non impostato.")

    log.info("Avvio sincronizzazione Gmail → HubSpot (ultimi %d giorni)", SEARCH_DAYS)

    gmail = get_gmail_service()
    senders = fetch_inbox_senders(gmail, days=SEARCH_DAYS)
    log.info("Mittenti unici trovati: %d", len(senders))

    results: list[SyncResult] = []
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}

    for sender in senders:
        try:
            result = sync_contact(sender)
            results.append(result)
            counts[result.status] += 1
            log.info(
                "[%s] %s | ID HubSpot: %s%s",
                result.status,
                result.email,
                result.contact_id,
                f" | {result.notes}" if result.notes else "",
            )
        except Exception as exc:
            log.error("Errore su %s: %s", sender.email, exc)
            results.append(SyncResult(email=sender.email, status="Errore", notes=str(exc)))

    # Summary
    log.info(
        "Completato. Creati: %d | Aggiornati: %d | Ignorati: %d",
        counts["Creato"], counts["Aggiornato"], counts["Ignorato"],
    )
    return results


if __name__ == "__main__":
    run_sync()
