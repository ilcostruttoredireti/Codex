#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors incoming Gmail inbox messages and syncs sender contacts to HubSpot.
Uses email address as unique key; creates new contacts or updates existing ones.

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 \
                google-api-python-client hubspot-api-client python-dotenv

Environment variables (.env or shell):
    HUBSPOT_ACCESS_TOKEN   - HubSpot private-app token with contacts r/w scope
    GMAIL_CREDENTIALS_JSON - Path to Google OAuth2 credentials.json
    GMAIL_TOKEN_JSON       - Path to store/load the OAuth2 token (default: token.json)
    SYNC_LOOKBACK_DAYS     - How many days back to scan Gmail (default: 1)
"""

import os
import re
import json
import logging
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional
from pathlib import Path

from dotenv import load_dotenv

# ── Google Gmail ──────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot ───────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDENTIALS = os.getenv("GMAIL_CREDENTIALS_JSON", "credentials.json")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN_JSON", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
LOOKBACK_DAYS = int(os.getenv("SYNC_LOOKBACK_DAYS", "1"))

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# Sender addresses that are automated/noreply — skip entirely
_SKIP_RE = re.compile(
    r"^(noreply|no-reply|no\.reply|do-not-reply|donotreply|"
    r"notify|notifications?|notify-noreply|"
    r"mailer-daemon|postmaster|bounce|"
    r"alerts?|automated|unsubscribe|newsletter)@",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data classes ──────────────────────────────────────────────────────────────
@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""


@dataclass
class SyncResult:
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── Gmail helpers ─────────────────────────────────────────────────────────────
def _gmail_service():
    """Return an authenticated Gmail API service object."""
    creds = None
    token_path = Path(GMAIL_TOKEN)
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(message: dict, name: str) -> str:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _parse_sender(raw_from: str) -> tuple[str, str, str, str]:
    """
    Parse a raw 'From' header value.
    Returns (email, first_name, last_name, company).
    """
    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""

    # Derive company from domain (strip common TLDs/subdomains)
    company = domain.replace("www.", "").split(".")[0].replace("-", " ").title()

    # Try to split display name into first / last
    parts = display_name.strip().split() if display_name else []
    first = parts[0] if len(parts) >= 1 else company
    last = " ".join(parts[1:]) if len(parts) >= 2 else ""

    return email_addr, first, last, company


def fetch_inbox_senders(lookback_days: int = 1) -> list[SenderContact]:
    """
    Return a deduplicated list of SenderContact for messages received in the
    last *lookback_days* days that are not from automated/noreply addresses.
    """
    svc = _gmail_service()
    query = f"in:inbox -from:me newer_than:{lookback_days}d"
    contacts: dict[str, SenderContact] = {}
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 50}
        if page_token:
            kwargs["pageToken"] = page_token

        resp = svc.users().messages().list(**kwargs).execute()
        message_ids = [m["id"] for m in resp.get("messages", [])]

        for msg_id in message_ids:
            msg = (
                svc.users()
                .messages()
                .get(userId="me", id=msg_id, format="metadata",
                     metadataHeaders=["From"])
                .execute()
            )
            raw_from = _header(msg, "From")
            if not raw_from:
                continue

            email_addr, first, last, company = _parse_sender(raw_from)

            if not email_addr or _SKIP_RE.match(email_addr.split("@")[0] + "@"):
                log.debug("Skipping automated sender: %s", email_addr)
                continue

            # Deduplicate by email (keep first occurrence)
            if email_addr not in contacts:
                contacts[email_addr] = SenderContact(
                    email=email_addr,
                    first_name=first,
                    last_name=last,
                    company=company,
                )

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Found %d unique senders to process.", len(contacts))
    return list(contacts.values())


# ── HubSpot helpers ───────────────────────────────────────────────────────────
def _hs_client():
    if not HUBSPOT_TOKEN:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(client, email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(
                        property_name="email",
                        operator="EQ",
                        value=email,
                    )
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    resp = client.crm.contacts.search_api.do_search(search_req)
    return resp.results[0] if resp.results else None


def _build_properties(sender: SenderContact, existing: Optional[dict]) -> dict:
    """
    Build the properties dict to send to HubSpot.
    Only include fields that are missing or empty on the existing record.
    """
    props = {"hs_analytics_source": CONTACT_SOURCE}

    def _missing(field_name: str) -> bool:
        if existing is None:
            return True
        return not (existing.properties or {}).get(field_name)

    if _missing("email"):
        props["email"] = sender.email
    if _missing("firstname") and sender.first_name:
        props["firstname"] = sender.first_name
    if _missing("lastname") and sender.last_name:
        props["lastname"] = sender.last_name
    if _missing("company") and sender.company:
        props["company"] = sender.company

    return props


def sync_contact(client, sender: SenderContact) -> SyncResult:
    """Create or update a single HubSpot contact. Returns a SyncResult."""
    existing = find_contact_by_email(client, sender.email)

    props = _build_properties(sender, existing)

    if existing is None:
        # ── Create new contact ────────────────────────────────────────────────
        props["email"] = sender.email
        obj = SimplePublicObjectInputForCreate(properties=props)
        try:
            created = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=obj
            )
            log.info("Creato  %s  →  ID %s", sender.email, created.id)
            return SyncResult("Creato", sender.email, created.id)
        except ApiException as exc:
            log.error("Errore creando %s: %s", sender.email, exc)
            return SyncResult("Ignorato", sender.email, reason=str(exc))

    # ── Update existing contact ───────────────────────────────────────────────
    # Only send properties that actually need updating
    update_props = {
        k: v
        for k, v in props.items()
        if k != "email"  # email is immutable via update
    }
    if not update_props:
        log.info("Ignorato %s  →  ID %s (nessun campo da aggiornare)", sender.email, existing.id)
        return SyncResult("Ignorato", sender.email, existing.id, "nessun campo da aggiornare")

    obj = SimplePublicObjectInput(properties=update_props)
    try:
        client.crm.contacts.basic_api.update(
            contact_id=existing.id,
            simple_public_object_input=obj,
        )
        log.info("Aggiornato %s  →  ID %s", sender.email, existing.id)
        return SyncResult("Aggiornato", sender.email, existing.id)
    except ApiException as exc:
        log.error("Errore aggiornando %s: %s", sender.email, exc)
        return SyncResult("Ignorato", sender.email, existing.id, str(exc))


# ── Main ───────────────────────────────────────────────────────────────────────
def run_sync(lookback_days: int = LOOKBACK_DAYS) -> list[SyncResult]:
    """Full sync pass: read Gmail → upsert HubSpot contacts."""
    log.info("=== Gmail → HubSpot sync avviato (ultimi %d giorni) ===", lookback_days)
    senders = fetch_inbox_senders(lookback_days)
    client = _hs_client()
    results: list[SyncResult] = []

    for sender in senders:
        result = sync_contact(client, sender)
        results.append(result)

    # ── Summary ───────────────────────────────────────────────────────────────
    created = sum(1 for r in results if r.status == "Creato")
    updated = sum(1 for r in results if r.status == "Aggiornato")
    ignored = sum(1 for r in results if r.status == "Ignorato")

    log.info(
        "=== Sync completato: %d Creati | %d Aggiornati | %d Ignorati ===",
        created, updated, ignored,
    )
    print(f"\n{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print("-" * 80)
    for r in results:
        print(f"{r.status:<12} {r.email:<45} {r.hubspot_id or '-'}")

    return results


if __name__ == "__main__":
    run_sync()
