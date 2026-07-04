#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages, extracts sender info, and creates/updates
contacts in HubSpot. Uses email as unique key to avoid duplicates.

Usage:
    python gmail_hubspot_sync.py [--since-days N]

Dependencies:
    pip install google-auth google-auth-oauthlib google-api-python-client hubspot-api-client
"""

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Skip list: addresses/domains that are automated and should never be synced
# ---------------------------------------------------------------------------
SKIP_LOCAL_PARTS = {
    "no-reply", "noreply", "no.reply", "donotreply", "do-not-reply",
    "notifications", "notify", "mailer-daemon", "postmaster", "bounce",
    "bounces", "support", "feedback", "alert", "alerts",
}
SKIP_DOMAINS = {
    "google.com", "gmail.com", "googlemail.com",
    "youtube.com", "patreon.com", "skool.com",
    "facebook.com", "linkedin.com", "twitter.com",
    "instagram.com", "tiktok.com",
}

STATE_FILE = Path(__file__).with_name(".sync_state.json")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SenderContact:
    email: str
    firstname: str
    lastname: str
    company: str
    domain: str


@dataclass
class SyncResult:
    email: str
    status: str          # Creato | Aggiornato | Ignorato
    hubspot_id: Optional[int]
    reason: str = ""


# ---------------------------------------------------------------------------
# State persistence (tracks processed thread IDs across runs)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_thread_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service():
    """Build an authenticated Gmail API service using OAuth2 credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
    creds = None
    token_file = Path("token.json")
    creds_file = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", "credentials.json"))

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), SCOPES)
            creds = flow.run_local_server(port=0)
        token_file.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_threads(service, since_days: int = 1) -> list[dict]:
    """Return threads from inbox received in the last `since_days` days."""
    after_date = (datetime.now(timezone.utc) - timedelta(days=since_days)).strftime("%Y/%m/%d")
    query = f"in:inbox -from:me -is:draft after:{after_date}"
    results = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        resp = service.users().threads().list(**kwargs).execute()
        results.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return results


def get_thread_sender(service, thread_id: str) -> tuple[str, str]:
    """Return (from_header_value, subject) for the first message of a thread."""
    thread = service.users().threads().get(
        userId="me", id=thread_id, format="metadata",
        metadataHeaders=["From", "Subject"]
    ).execute()
    first_msg = thread["messages"][0]
    headers = {h["name"]: h["value"] for h in first_msg.get("payload", {}).get("headers", [])}
    return headers.get("From", ""), headers.get("Subject", "")


# ---------------------------------------------------------------------------
# Sender parsing
# ---------------------------------------------------------------------------

def should_skip_email(email: str) -> bool:
    """Return True if this address is an automated sender we should ignore."""
    email = email.lower().strip()
    local, _, domain = email.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    for part in SKIP_LOCAL_PARTS:
        if part in local:
            return True
    return False


def domain_to_company(domain: str) -> str:
    """Convert a domain like 'raffaprivatejet.com' to 'Raffa Private Jet'."""
    name = domain.split(".")[0]
    # Split on common word-boundary patterns
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name)   # camelCase
    name = re.sub(r"[-_]", " ", name)
    return name.title()


def parse_sender(from_header: str) -> Optional[SenderContact]:
    """Parse a From: header into a SenderContact, or return None to skip."""
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None
    if should_skip_email(email):
        return None

    local, _, domain = email.partition("@")
    company = domain_to_company(domain)

    # Try to extract first/last from display name
    firstname, lastname = "", ""
    name = display_name.strip().strip('"')
    if name:
        parts = name.split(maxsplit=1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""
    else:
        # Fallback: use local part as first name
        firstname = local.replace(".", " ").replace("-", " ").replace("_", " ").title()

    return SenderContact(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
    )


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def build_hubspot_client():
    from hubspot import HubSpot
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN environment variable not set")
    return HubSpot(access_token=token)


def find_contact_by_email(hs, email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "lead_source", "hs_lead_status"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.total > 0:
        return resp.results[0]
    return None


def create_contact(hs, contact: SenderContact) -> int:
    """Create a new HubSpot contact and return its ID."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    props = {
        "email": contact.email,
        "firstname": contact.firstname,
        "company": contact.company,
        "hs_lead_status": "NEW",
        "hs_analytics_source": "OTHER_CAMPAIGNS",
        "hs_analytics_source_data_1": "Inbound Gmail",
    }
    if contact.lastname:
        props["lastname"] = contact.lastname

    obj = SimplePublicObjectInputForCreate(properties=props)
    result = hs.crm.contacts.basic_api.create(simple_public_object_input_for_create=obj)
    return int(result.id)


def update_contact_if_needed(hs, existing: dict, contact: SenderContact) -> bool:
    """Update missing fields on an existing contact. Returns True if changes made."""
    from hubspot.crm.contacts import SimplePublicObjectInput

    current = existing.properties
    updates = {}

    if not current.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not current.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not current.get("company") and contact.company:
        updates["company"] = contact.company
    if not current.get("hs_analytics_source_data_1"):
        updates["hs_analytics_source_data_1"] = "Inbound Gmail"
    if not current.get("hs_lead_status"):
        updates["hs_lead_status"] = "NEW"

    if not updates:
        return False

    obj = SimplePublicObjectInput(properties=updates)
    hs.crm.contacts.basic_api.update(
        contact_id=existing.id,
        simple_public_object_input=obj,
    )
    return True


# ---------------------------------------------------------------------------
# Core sync loop
# ---------------------------------------------------------------------------

def sync_email_to_hubspot(
    gmail_service,
    hs_client,
    thread: dict,
    processed_ids: set,
) -> SyncResult:
    thread_id = thread["id"]

    if thread_id in processed_ids:
        return SyncResult(email="—", status="Ignorato", hubspot_id=None, reason="già processato")

    from_header, subject = get_thread_sender(gmail_service, thread_id)
    contact = parse_sender(from_header)

    if contact is None:
        return SyncResult(email=from_header or "—", status="Ignorato", hubspot_id=None, reason="mittente automatico")

    existing = find_contact_by_email(hs_client, contact.email)

    if existing is None:
        contact_id = create_contact(hs_client, contact)
        return SyncResult(email=contact.email, status="Creato", hubspot_id=contact_id)

    changed = update_contact_if_needed(hs_client, existing, contact)
    status = "Aggiornato" if changed else "Ignorato"
    return SyncResult(email=contact.email, status=status, hubspot_id=int(existing.id),
                      reason="" if changed else "nessun campo mancante")


def run_sync(since_days: int = 1) -> list[SyncResult]:
    state = load_state()
    processed_ids = set(state.get("processed_thread_ids", []))

    gmail = build_gmail_service()
    hs = build_hubspot_client()

    threads = fetch_inbox_threads(gmail, since_days=since_days)
    log.info(f"Trovati {len(threads)} thread Gmail (ultimi {since_days} giorni)")

    results: list[SyncResult] = []
    newly_processed: list[str] = []

    for thread in threads:
        try:
            result = sync_email_to_hubspot(gmail, hs, thread, processed_ids)
            results.append(result)
            newly_processed.append(thread["id"])
            status_icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}.get(result.status, "")
            log.info(f"{status_icon} {result.status:12s} {result.email}  (HubSpot ID: {result.hubspot_id})")
        except Exception as exc:
            log.error(f"Errore su thread {thread['id']}: {exc}")

    # Persist state (keep last 10 000 IDs)
    all_ids = list(processed_ids) + newly_processed
    state["processed_thread_ids"] = all_ids[-10_000:]
    save_state(state)

    return results


def print_report(results: list[SyncResult]) -> None:
    created = [r for r in results if r.status == "Creato"]
    updated = [r for r in results if r.status == "Aggiornato"]
    ignored = [r for r in results if r.status == "Ignorato"]

    print("\n" + "=" * 60)
    print(f"  REPORT SINCRONIZZAZIONE Gmail → HubSpot")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)
    print(f"  Totale email processate : {len(results)}")
    print(f"  ✅ Creati               : {len(created)}")
    print(f"  🔄 Aggiornati           : {len(updated)}")
    print(f"  ⏭️  Ignorati             : {len(ignored)}")
    print("=" * 60)

    if created or updated:
        print("\n  DETTAGLIO MODIFICHE:")
        print(f"  {'Stato':<12} {'Email':<40} {'HubSpot ID'}")
        print(f"  {'-'*12} {'-'*40} {'-'*12}")
        for r in created + updated:
            print(f"  {r.status:<12} {r.email:<40} {r.hubspot_id}")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sincronizza mittenti Gmail → HubSpot CRM")
    parser.add_argument("--since-days", type=int, default=1,
                        help="Finestra temporale in giorni (default: 1)")
    args = parser.parse_args()

    results = run_sync(since_days=args.since_days)
    print_report(results)
