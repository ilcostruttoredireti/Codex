"""
Gmail → HubSpot contact sync.

Fetches recent Gmail inbox emails, extracts real human senders,
then creates or updates HubSpot contacts using email as the unique key.
"""

import os
import re
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

import httpx
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
HUBSPOT_TOKEN = os.environ["HUBSPOT_API_TOKEN"]
SEARCH_DAYS = int(os.getenv("SEARCH_DAYS", "1"))
CONTACT_TAG = "Inbound Gmail"
# Note: HubSpot analytics source fields are read-only.
# Source tracking is done via timeline notes (engagements API).

# Patterns that identify automated / no-reply senders to skip
_SKIP_PATTERNS = re.compile(
    r"(no.?reply|noreply|donotreply|mailer.daemon|postmaster|notifications?|"
    r"newsletter|support|security|billing|payment|order|receipt|alert|"
    r"notification|notify|bounce|daemon|unsubscribe|automated|auto-confirm)",
    re.IGNORECASE,
)

# Well-known bulk-mail domains to skip
_SKIP_DOMAINS = {
    "facebookmail.com", "twitteremail.com", "linkedin.com",
    "bounce.linkedin.com", "em.linkedin.com", "notifications.google.com",
    "accounts.google.com", "googleplay.com",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    note: str = ""


@dataclass
class Sender:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""

    @classmethod
    def from_header(cls, raw: str) -> "Sender":
        """Parse a raw From header like 'John Doe <john@example.com>'."""
        raw = raw.strip()
        match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', raw)
        if match:
            display, addr = match.group(1).strip(), match.group(2).strip().lower()
        else:
            addr = raw.lower()
            display = ""
        parts = display.split() if display else []
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        domain = addr.split("@")[-1] if "@" in addr else ""
        company = _domain_to_company(domain)
        return cls(email=addr, firstname=first, lastname=last, company=company)


def _domain_to_company(domain: str) -> str:
    """Best-effort company name from domain (strips TLD and capitalises)."""
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]
        return name.replace("-", " ").title()
    return domain


def _is_automated(sender: Sender) -> bool:
    local = sender.email.split("@")[0]
    domain = sender.email.split("@")[-1] if "@" in sender.email else ""
    if _SKIP_PATTERNS.search(local):
        return True
    if domain in _SKIP_DOMAINS:
        return True
    return False


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_senders(days: int = 1) -> list[Sender]:
    """Return deduplicated real-human Sender objects from recent inbox."""
    svc = _gmail_service()
    query = f"in:inbox newer_than:{days}d -from:me"
    results = svc.users().messages().list(userId="me", q=query, maxResults=200).execute()
    messages = results.get("messages", [])

    seen: dict[str, Sender] = {}
    for msg_ref in messages:
        msg = svc.users().messages().get(
            userId="me", id=msg_ref["id"], format="metadata",
            metadataHeaders=["From"],
        ).execute()
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")
        if not raw_from:
            continue
        sender = Sender.from_header(raw_from)
        if not sender.email or sender.email in seen:
            continue
        if _is_automated(sender):
            log.debug("Skipped automated sender: %s", sender.email)
            continue
        seen[sender.email] = sender

    log.info("Found %d unique real senders in last %d day(s)", len(seen), days)
    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

HS_BASE = "https://api.hubapi.com"
_hs_headers = lambda: {
    "Authorization": f"Bearer {HUBSPOT_TOKEN}",
    "Content-Type": "application/json",
}


def hs_find_contact(email: str) -> Optional[dict]:
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
    }
    r = httpx.post(f"{HS_BASE}/crm/v3/objects/contacts/search", headers=_hs_headers(), json=payload)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: Sender) -> dict:
    props: dict = {"email": sender.email}
    if sender.firstname:
        props["firstname"] = sender.firstname
    if sender.lastname:
        props["lastname"] = sender.lastname
    if sender.company:
        props["company"] = sender.company
    r = httpx.post(f"{HS_BASE}/crm/v3/objects/contacts", headers=_hs_headers(), json={"properties": props})
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, updates: dict) -> dict:
    r = httpx.patch(
        f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
    )
    r.raise_for_status()
    return r.json()


def hs_add_timeline_activity(contact_id: str, sender: Sender) -> None:
    """Log an 'email received' note on the contact timeline via engagement API."""
    payload = {
        "engagement": {"active": True, "type": "NOTE"},
        "associations": {"contactIds": [int(contact_id)]},
        "metadata": {"body": f"Email in arrivo da {sender.email} — fonte: {CONTACT_TAG}"},
    }
    r = httpx.post(f"{HS_BASE}/engagements/v1/engagements", headers=_hs_headers(), json=payload)
    if r.status_code not in (200, 201):
        log.warning("Timeline activity failed for %s: %s", contact_id, r.text)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def sync_sender(sender: Sender, add_timeline: bool = True) -> SyncResult:
    existing = hs_find_contact(sender.email)

    if existing is None:
        created = hs_create_contact(sender)
        cid = created["id"]
        if add_timeline:
            hs_add_timeline_activity(cid, sender)
        return SyncResult(SyncStatus.CREATED, sender.email, cid)

    cid = existing["id"]
    props = existing.get("properties", {})
    updates: dict = {}

    if not props.get("firstname") and sender.firstname:
        updates["firstname"] = sender.firstname
    if not props.get("lastname") and sender.lastname:
        updates["lastname"] = sender.lastname
    if not props.get("company") and sender.company:
        updates["company"] = sender.company

    if updates:
        hs_update_contact(cid, updates)
        if add_timeline:
            hs_add_timeline_activity(cid, sender)
        return SyncResult(SyncStatus.UPDATED, sender.email, cid, note=f"campi aggiornati: {list(updates.keys())}")

    return SyncResult(SyncStatus.IGNORED, sender.email, cid, note="nessuna modifica necessaria")


def run_sync(days: int = SEARCH_DAYS) -> list[SyncResult]:
    senders = fetch_senders(days)
    results: list[SyncResult] = []
    for sender in senders:
        try:
            result = sync_sender(sender)
            log.info("[%s] %s — HubSpot ID: %s %s",
                     result.status.value, result.email, result.hubspot_id, result.note)
            results.append(result)
        except Exception as exc:
            log.error("Error processing %s: %s", sender.email, exc)
            results.append(SyncResult(SyncStatus.IGNORED, sender.email, note=str(exc)))
        time.sleep(0.1)  # gentle rate limiting

    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
    log.info("Sync completato — Creati: %d | Aggiornati: %d | Ignorati: %d", created, updated, ignored)
    return results


if __name__ == "__main__":
    run_sync()
