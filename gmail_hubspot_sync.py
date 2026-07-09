"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox, extracts senders, and syncs them to HubSpot CRM.
Uses email as the unique key to avoid duplicates.

Required environment variables:
  HUBSPOT_ACCESS_TOKEN   — HubSpot private app token
  GMAIL_CREDENTIALS_FILE — path to OAuth2 credentials JSON from Google Cloud Console
  GMAIL_TOKEN_FILE       — path where the OAuth2 token will be stored (default: token.json)
  LOOKBACK_HOURS         — how many hours back to scan (default: 24)
"""

import os
import re
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from typing import Optional

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# Gmail OAuth scopes (read-only inbox is sufficient)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Sender patterns that should never become contacts
SKIP_PATTERNS = re.compile(
    r"^(noreply|no-reply|do-not-reply|donotreply|bounce|mailer-daemon|postmaster|"
    r"notifications?|alerts?|newsletter|automated?|robot|daemon)@",
    re.IGNORECASE,
)

# Company name overrides keyed on email domain
DOMAIN_COMPANY_MAP = {
    "gmail.com": None,
    "yahoo.com": None,
    "hotmail.com": None,
    "outlook.com": None,
    "icloud.com": None,
    "live.com": None,
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class SenderInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    reason: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _domain(email: str) -> str:
    return email.split("@")[-1].lower()


def _company_from_domain(domain: str) -> Optional[str]:
    if domain in DOMAIN_COMPANY_MAP:
        return DOMAIN_COMPANY_MAP[domain]
    # Strip common subdomains (mail., send., info., etc.)
    parts = domain.split(".")
    root = ".".join(parts[-2:]) if len(parts) >= 2 else domain
    # Capitalise first word of root domain as best-guess company name
    company = root.split(".")[0].replace("-", " ").title()
    return company


def _parse_name(local_part: str) -> tuple[Optional[str], Optional[str]]:
    """Try to extract first/last name from the local part of an email address."""
    # e.g. si.quan → ("Si", "Quan"); john.doe → ("John", "Doe")
    parts = re.split(r"[._\-]", local_part)
    if len(parts) >= 2 and all(p.isalpha() for p in parts[:2]):
        return parts[0].capitalize(), parts[1].capitalize()
    if len(parts) == 1 and parts[0].isalpha():
        return parts[0].capitalize(), None
    return None, None


def extract_sender(raw_from: str) -> Optional[SenderInfo]:
    """Parse a raw From header value into a SenderInfo. Returns None if it should be skipped."""
    display_name, email = parseaddr(raw_from)
    if not email or "@" not in email:
        return None
    email = email.lower().strip()

    if SKIP_PATTERNS.match(email.split("@")[0] + "@"):
        return None

    domain = _domain(email)
    company = _company_from_domain(domain)

    # Derive first/last name
    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name:
        name_parts = display_name.strip().split()
        if name_parts:
            first_name = name_parts[0]
            last_name = " ".join(name_parts[1:]) if len(name_parts) > 1 else None
    else:
        first_name, last_name = _parse_name(email.split("@")[0])

    return SenderInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------


def _gmail_credentials(credentials_file: str, token_file: str) -> Credentials:
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as fh:
            fh.write(creds.to_json())
    return creds


def fetch_inbox_senders(
    credentials_file: str,
    token_file: str = "token.json",
    lookback_hours: int = 24,
) -> list[SenderInfo]:
    """Return deduplicated list of valid senders from the last `lookback_hours` hours."""
    creds = _gmail_credentials(credentials_file, token_file)
    service = build("gmail", "v1", credentials=creds)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    # Gmail query: inbox, not sent by me, newer than cutoff
    query = f"in:inbox -from:me after:{int(cutoff.timestamp())}"

    results = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=100)
        .execute()
    )
    messages = results.get("messages", [])

    seen_emails: set[str] = set()
    senders: list[SenderInfo] = []

    for msg_stub in messages:
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=msg_stub["id"], format="metadata", metadataHeaders=["From"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        raw_from = headers.get("From", "")

        sender = extract_sender(raw_from)
        if sender is None or sender.email in seen_emails:
            continue
        seen_emails.add(sender.email)
        senders.append(sender)
        log.debug("Found sender: %s", sender.email)

    log.info("Found %d unique valid senders in the last %dh", len(senders), lookback_hours)
    return senders


# ---------------------------------------------------------------------------
# HubSpot client (using plain httpx to avoid SDK version pinning)
# ---------------------------------------------------------------------------


class HubSpotClient:
    BASE = "https://api.hubapi.com"

    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def search_contact_by_email(self, email: str) -> Optional[dict]:
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        }
        resp = httpx.post(
            f"{self.BASE}/crm/v3/objects/contacts/search",
            headers=self._headers,
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        resp = httpx.post(
            f"{self.BASE}/crm/v3/objects/contacts",
            headers=self._headers,
            json={"properties": props},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, props: dict) -> dict:
        resp = httpx.patch(
            f"{self.BASE}/crm/v3/objects/contacts/{contact_id}",
            headers=self._headers,
            json={"properties": props},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def add_note(self, contact_id: str, body: str) -> None:
        """Create a note and associate it with the given contact."""
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        note_payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(now_ms),
            }
        }
        resp = httpx.post(
            f"{self.BASE}/crm/v3/objects/notes",
            headers=self._headers,
            json=note_payload,
            timeout=15,
        )
        resp.raise_for_status()
        note_id = resp.json()["id"]
        # Associate note → contact (associationTypeId 202 = note to contact)
        assoc_resp = httpx.put(
            f"{self.BASE}/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}/202",
            headers=self._headers,
            timeout=15,
        )
        assoc_resp.raise_for_status()


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------


def _build_contact_props(sender: SenderInfo) -> dict:
    props: dict = {"email": sender.email}
    if sender.first_name:
        props["firstname"] = sender.first_name
    if sender.last_name:
        props["lastname"] = sender.last_name
    if sender.company:
        props["company"] = sender.company
    return props


_GMAIL_NOTE = "📧 Email ricevuta via Gmail inbox. Fonte contatto: Gmail. Tag: Inbound Gmail"


def sync_sender(hs: HubSpotClient, sender: SenderInfo) -> SyncResult:
    existing = hs.search_contact_by_email(sender.email)

    if existing is None:
        props = _build_contact_props(sender)
        created = hs.create_contact(props)
        contact_id = created["id"]
        hs.add_note(contact_id, _GMAIL_NOTE)
        log.info("CREATO   %s → ID %s", sender.email, contact_id)
        return SyncResult(email=sender.email, hubspot_id=contact_id, status="Creato")

    contact_id = existing["id"]
    existing_props = existing.get("properties", {})

    # Fill in any missing fields — never overwrite what's already there
    updates: dict = {}
    if not existing_props.get("firstname") and sender.first_name:
        updates["firstname"] = sender.first_name
    if not existing_props.get("lastname") and sender.last_name:
        updates["lastname"] = sender.last_name
    if not existing_props.get("company") and sender.company:
        updates["company"] = sender.company

    if updates:
        hs.update_contact(contact_id, updates)
        hs.add_note(contact_id, _GMAIL_NOTE)
        log.info("AGGIORNATO %s → ID %s (%s)", sender.email, contact_id, list(updates.keys()))
        return SyncResult(email=sender.email, hubspot_id=contact_id, status="Aggiornato", reason=str(list(updates.keys())))

    log.info("IGNORATO %s → ID %s (nessuna modifica)", sender.email, contact_id)
    return SyncResult(email=sender.email, hubspot_id=contact_id, status="Ignorato", reason="già aggiornato")


def run_sync(
    hubspot_token: str,
    gmail_credentials_file: str,
    gmail_token_file: str = "token.json",
    lookback_hours: int = 24,
) -> list[SyncResult]:
    hs = HubSpotClient(hubspot_token)
    senders = fetch_inbox_senders(gmail_credentials_file, gmail_token_file, lookback_hours)

    results: list[SyncResult] = []
    for sender in senders:
        try:
            result = sync_sender(hs, sender)
            results.append(result)
        except Exception as exc:
            log.error("Errore su %s: %s", sender.email, exc)
            results.append(SyncResult(email=sender.email, hubspot_id=None, status="Errore", reason=str(exc)))

    # Summary
    created = sum(1 for r in results if r.status == "Creato")
    updated = sum(1 for r in results if r.status == "Aggiornato")
    ignored = sum(1 for r in results if r.status == "Ignorato")
    errors  = sum(1 for r in results if r.status == "Errore")
    log.info("Sync completato — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
             created, updated, ignored, errors)
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    creds_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    hours = int(os.environ.get("LOOKBACK_HOURS", "24"))

    if not token:
        raise SystemExit("Imposta la variabile d'ambiente HUBSPOT_ACCESS_TOKEN")
    if not os.path.exists(creds_file):
        raise SystemExit(f"File credenziali Gmail non trovato: {creds_file}")

    results = run_sync(token, creds_file, token_file, hours)

    print("\n=== RIEPILOGO SYNC ===")
    print(f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print("-" * 75)
    for r in results:
        print(f"{r.status:<12} {r.email:<45} {r.hubspot_id or 'N/A'}")
