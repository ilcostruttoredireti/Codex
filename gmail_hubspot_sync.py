"""
Gmail → HubSpot Contact Sync
Monitors inbox emails, extracts sender info, and syncs contacts to HubSpot.
Avoids duplicates using email as the unique key.
"""

import re
import json
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------

@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    raw_name: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@", 1)[-1] if "@" in self.email else ""

    @property
    def is_personal_domain(self) -> bool:
        personal = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                    "libero.it", "virgilio.it", "tiscali.it"}
        return self.domain.lower() in personal


@dataclass
class SyncResult:
    email: str
    hubspot_id: Optional[str]
    status: str          # CREATO | AGGIORNATO | IGNORATO
    detail: str = ""


# ------------------------------------------------------------------
# Parsing helpers
# ------------------------------------------------------------------

_FROM_PATTERN = re.compile(
    r'(?:Da\s+"?([^"<]+?)"?\s+)?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)

_FWD_FROM_PATTERN = re.compile(
    r'Da:\s+([^<\n]+?)\s+<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
    re.IGNORECASE,
)

SKIP_DOMAINS = {
    "facebookmail.com", "twitter.com", "linkedin.com", "notifications.google.com",
    "mailer.linkedin.com", "bounce.hatena.ne.jp", "noreply.github.com",
}

SKIP_LOCAL_PARTS = {"noreply", "no-reply", "mailer-daemon", "postmaster", "bounce"}


def parse_sender(thread_snippet: str) -> Optional[SenderContact]:
    """Extract sender from a Gmail thread snippet."""
    # Try forwarded message pattern first
    m = _FWD_FROM_PATTERN.search(thread_snippet)
    if m:
        raw_name = m.group(1).strip()
        email = m.group(2).strip().lower()
    else:
        m = _FROM_PATTERN.search(thread_snippet)
        if not m:
            return None
        raw_name = (m.group(1) or "").strip()
        email = m.group(2).strip().lower()

    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[-1].lower()
    local = email.split("@", 1)[0].lower()
    if domain in SKIP_DOMAINS or local in SKIP_LOCAL_PARTS:
        return None

    contact = SenderContact(email=email, raw_name=raw_name)
    _split_name(contact, raw_name, domain)
    return contact


def _split_name(contact: SenderContact, raw_name: str, domain: str) -> None:
    """Heuristically populate first/last name and company."""
    clean = raw_name.strip().strip('"')
    if not clean:
        return

    # If name looks like an office/service, treat it as company
    office_keywords = (
        "ufficio", "stampa", "comunicazione", "press", "media",
        "service", "info", "servizi", "redazione",
    )
    lower = clean.lower()
    is_office = any(kw in lower for kw in office_keywords)

    if is_office:
        contact.company = clean
        parts = clean.split()
        contact.first_name = parts[0] if parts else clean
        contact.last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    else:
        parts = clean.split()
        if len(parts) >= 2:
            contact.first_name = parts[0]
            contact.last_name = " ".join(parts[1:])
        else:
            contact.first_name = clean

    # Infer company from professional domain if not already set
    if not contact.company and not contact.is_personal_domain:
        contact.company = domain.split(".")[0].title()


def deduplicate(contacts: list[SenderContact]) -> dict[str, SenderContact]:
    """Return dict keyed by email, last occurrence wins."""
    seen: dict[str, SenderContact] = {}
    for c in contacts:
        seen[c.email] = c
    return seen


# ------------------------------------------------------------------
# HubSpot sync logic (pseudo-API wrappers — replace with real SDK calls)
# ------------------------------------------------------------------

class HubSpotClient:
    """
    Thin wrapper around the HubSpot Contacts API.
    Replace the stub methods with real calls (e.g. hubspot SDK or requests).
    """

    BASE_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
    SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"

    def __init__(self, access_token: str):
        try:
            import hubspot
            from hubspot.crm.contacts import SimplePublicObjectInputForCreate
            self._sdk = hubspot.Client.create(access_token=access_token)
        except ImportError:
            self._sdk = None
        self._token = access_token

    def search_by_email(self, email: str) -> Optional[dict]:
        import requests
        payload = {
            "filterGroups": [{"filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]}],
            "properties": ["email", "firstname", "lastname", "company",
                           "hs_analytics_source", "hs_lead_status"],
            "limit": 1,
        }
        resp = requests.post(
            self.SEARCH_URL,
            json=payload,
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, contact: SenderContact) -> dict:
        import requests
        props = {
            "email": contact.email,
            "firstname": contact.first_name or contact.email.split("@")[0],
            "lastname": contact.last_name,
            "hs_analytics_source": "EMAIL_MARKETING",
            "hs_lead_status": "NEW",
        }
        if contact.company:
            props["company"] = contact.company
        resp = requests.post(
            self.BASE_URL,
            json={"properties": props},
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, updates: dict) -> dict:
        import requests
        resp = requests.patch(
            f"{self.BASE_URL}/{contact_id}",
            json={"properties": updates},
            headers={"Authorization": f"Bearer {self._token}",
                     "Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


# ------------------------------------------------------------------
# Core sync routine
# ------------------------------------------------------------------

def sync_contact(
    client: HubSpotClient,
    contact: SenderContact,
) -> SyncResult:
    existing = client.search_by_email(contact.email)
    if existing is None:
        created = client.create_contact(contact)
        return SyncResult(
            email=contact.email,
            hubspot_id=created.get("id"),
            status="CREATO",
        )

    contact_id = existing["id"]
    props = existing.get("properties", {})
    updates: dict = {}

    # Fill missing fields
    if not props.get("firstname") and contact.first_name:
        updates["firstname"] = contact.first_name
    if not props.get("lastname") and contact.last_name:
        updates["lastname"] = contact.last_name
    if not props.get("company") and contact.company:
        updates["company"] = contact.company
    if not props.get("hs_analytics_source"):
        updates["hs_analytics_source"] = "EMAIL_MARKETING"
    if not props.get("hs_lead_status"):
        updates["hs_lead_status"] = "NEW"

    if updates:
        client.update_contact(contact_id, updates)
        return SyncResult(
            email=contact.email,
            hubspot_id=contact_id,
            status="AGGIORNATO",
            detail=f"updated fields: {list(updates.keys())}",
        )

    return SyncResult(
        email=contact.email,
        hubspot_id=contact_id,
        status="IGNORATO",
    )


# ------------------------------------------------------------------
# Gmail poller (uses google-api-python-client)
# ------------------------------------------------------------------

def fetch_inbox_senders(
    service,            # googleapiclient.discovery resource
    max_results: int = 50,
    newer_than_days: int = 7,
) -> list[SenderContact]:
    """Fetch recent inbox threads and extract unique senders."""
    query = f"in:inbox -from:me newer_than:{newer_than_days}d"
    threads = (
        service.users()
        .threads()
        .list(userId="me", q=query, maxResults=max_results)
        .execute()
        .get("threads", [])
    )

    contacts: list[SenderContact] = []
    for t in threads:
        thread = (
            service.users()
            .threads()
            .get(userId="me", id=t["id"], format="metadata",
                 metadataHeaders=["From"])
            .execute()
        )
        for msg in thread.get("messages", []):
            for header in msg.get("payload", {}).get("headers", []):
                if header["name"].lower() == "from":
                    c = _parse_header_from(header["value"])
                    if c:
                        contacts.append(c)
                    break

    unique = deduplicate(contacts)
    log.info("Found %d unique senders in inbox.", len(unique))
    return list(unique.values())


def _parse_header_from(header_value: str) -> Optional[SenderContact]:
    """Parse RFC 5322 'From' header like 'Name <email>' or bare 'email'."""
    m = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', header_value)
    if m:
        raw_name, email = m.group(1).strip(), m.group(2).strip().lower()
    else:
        email = header_value.strip().lower()
        raw_name = ""

    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[-1].lower()
    local = email.split("@", 1)[0].lower()
    if domain in SKIP_DOMAINS or local in SKIP_LOCAL_PARTS:
        return None

    contact = SenderContact(email=email, raw_name=raw_name)
    _split_name(contact, raw_name, domain)
    return contact


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def run_sync(hubspot_token: str, gmail_service, newer_than_days: int = 7):
    client = HubSpotClient(hubspot_token)
    senders = fetch_inbox_senders(gmail_service, newer_than_days=newer_than_days)

    results: list[SyncResult] = []
    for sender in senders:
        try:
            result = sync_contact(client, sender)
            results.append(result)
            log.info("[%s] %s (id=%s) %s",
                     result.status, result.email, result.hubspot_id, result.detail)
        except Exception as exc:
            log.error("Failed to sync %s: %s", sender.email, exc)
            results.append(SyncResult(email=sender.email, hubspot_id=None,
                                      status="ERRORE", detail=str(exc)))
        time.sleep(0.1)  # avoid rate limits

    # Summary
    created = sum(1 for r in results if r.status == "CREATO")
    updated = sum(1 for r in results if r.status == "AGGIORNATO")
    ignored = sum(1 for r in results if r.status == "IGNORATO")
    errors  = sum(1 for r in results if r.status == "ERRORE")
    log.info("Sync complete: %d created, %d updated, %d ignored, %d errors",
             created, updated, ignored, errors)
    return results


if __name__ == "__main__":
    import os
    import sys

    # Credentials via environment variables
    hs_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hs_token:
        sys.exit("HUBSPOT_ACCESS_TOKEN not set")

    # Build Gmail service from service-account JSON or OAuth credentials
    creds_file = os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build

        SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
        creds = None
        if os.path.exists("token.json"):
            creds = Credentials.from_authorized_user_file("token.json", SCOPES)
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(creds_file, SCOPES)
            creds = flow.run_local_server(port=0)
            with open("token.json", "w") as f:
                f.write(creds.to_json())

        gmail_svc = build("gmail", "v1", credentials=creds)
    except ImportError:
        sys.exit("Install: pip install google-auth google-auth-oauthlib google-api-python-client")

    newer = int(os.environ.get("NEWER_THAN_DAYS", "7"))
    run_sync(hs_token, gmail_svc, newer_than_days=newer)
