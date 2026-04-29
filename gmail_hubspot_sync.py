"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and upserts each new sender as a HubSpot contact.

Usage:
    python gmail_hubspot_sync.py [--once]   # --once skips the polling loop

Environment variables (see .env.example):
    GMAIL_CREDENTIALS_FILE   path to OAuth2 client_secret JSON
    GMAIL_TOKEN_FILE         path where the token is cached
    HUBSPOT_API_KEY          HubSpot private app token
    SYNC_INTERVAL_SECONDS    polling interval (default 300)
    SKIP_DOMAINS             comma-separated domains to ignore (e.g. gmail.com)
    MAX_RESULTS              max threads per Gmail poll (default 50)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import hubspot
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import PublicObjectSearchRequest

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "50"))
CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# Domains whose senders are never synced (automated / own addresses)
_SKIP_DOMAINS_ENV = os.getenv(
    "SKIP_DOMAINS",
    "noreply.google.com,accounts.google.com,no-reply.accounts.google.com",
)
SKIP_DOMAINS: set[str] = {d.strip().lower() for d in _SKIP_DOMAINS_ENV.split(",") if d.strip()}

# Prefixes that indicate automated senders (exact or prefix match on local-part)
SKIP_LOCAL_PREFIXES = {"no-reply", "noreply", "do-not-reply", "donotreply", "mailer-daemon"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Sender:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""

    @property
    def domain(self) -> str:
        return self.email.split("@", 1)[-1].lower()

    def is_automated(self) -> bool:
        local = self.email.split("@", 1)[0].lower()
        if self.domain in SKIP_DOMAINS:
            return True
        return any(local.startswith(p) for p in SKIP_LOCAL_PREFIXES)


@dataclass
class SyncResult:
    email: str
    status: str          # "created" | "updated" | "ignored"
    contact_id: str = ""
    reason: str = ""


# ---------------------------------------------------------------------------
# Name / company helpers
# ---------------------------------------------------------------------------

_COMPANY_FROM_DOMAIN_SKIP = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
                              "libero.it", "virgilio.it", "tiscali.it", "icloud.com"}


def _parse_display_name(display_name: str) -> tuple[str, str]:
    """Split 'Firstname Lastname' into (first, last), handling single names."""
    parts = display_name.strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _names_from_email_address(local: str) -> tuple[str, str]:
    """Guess first/last name from local part like 'mario.rossi' or 'mario_rossi'."""
    local = re.sub(r"[._-]", " ", local).strip()
    parts = local.split(None, 1)
    if not parts:
        return "", ""
    first = parts[0].capitalize()
    last = parts[1].capitalize() if len(parts) > 1 else ""
    return first, last


def _company_from_domain(domain: str) -> str:
    """Derive a readable company name from a domain (best effort)."""
    if domain in _COMPANY_FROM_DOMAIN_SKIP:
        return ""
    # Strip TLD(s) and capitalise each word
    parts = domain.split(".")
    # Remove known generic TLDs
    clean = [p for p in parts if p not in ("com", "it", "net", "org", "gov", "eu", "co")]
    name = " ".join(p.capitalize() for p in clean)
    return name


def build_sender(raw_email: str, display_name: str) -> Sender:
    """Construct a Sender from a raw email address and optional display name."""
    raw_email = raw_email.strip().lower()
    local = raw_email.split("@", 1)[0]
    domain = raw_email.split("@", 1)[-1]

    if display_name and display_name.strip():
        first, last = _parse_display_name(display_name)
    else:
        first, last = _names_from_email_address(local)

    company = _company_from_domain(domain)

    return Sender(email=raw_email, firstname=first, lastname=last, company=company)


# ---------------------------------------------------------------------------
# Gmail client
# ---------------------------------------------------------------------------

def get_gmail_service():
    creds: Credentials | None = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_from_header(from_header: str) -> tuple[str, str]:
    """
    Parse 'Display Name <email@domain.com>' or 'email@domain.com'.
    Returns (display_name, email).
    """
    m = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>', from_header.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # plain address
    return "", from_header.strip()


def fetch_inbox_senders(service, query: str = "in:inbox") -> list[Sender]:
    """Return unique Sender objects from the most recent inbox messages."""
    results = (
        service.users()
        .threads()
        .list(userId="me", q=query, maxResults=MAX_RESULTS)
        .execute()
    )
    threads = results.get("threads", [])
    seen: dict[str, Sender] = {}

    for thread_meta in threads:
        thread = (
            service.users()
            .threads()
            .get(userId="me", threadId=thread_meta["id"], format="metadata",
                 metadataHeaders=["From", "To", "Subject"])
            .execute()
        )
        for msg in thread.get("messages", []):
            headers = msg.get("payload", {}).get("headers", [])
            from_hdr = _header(headers, "From")
            if not from_hdr:
                continue
            display_name, email_addr = _parse_from_header(from_hdr)
            if not email_addr or "@" not in email_addr:
                continue
            email_addr = email_addr.lower()
            if email_addr in seen:
                continue
            sender = build_sender(email_addr, display_name)
            if sender.is_automated():
                log.debug("Skipping automated address: %s", email_addr)
                continue
            seen[email_addr] = sender

    return list(seen.values())


# ---------------------------------------------------------------------------
# HubSpot client
# ---------------------------------------------------------------------------

def get_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_API_KEY:
        raise RuntimeError("HUBSPOT_API_KEY is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


def _find_contact_by_email(client: hubspot.Client, email: str) -> dict | None:
    """Return the existing HubSpot contact dict or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status",
                    "leadsource", "notes_last_contacted"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.total > 0:
            return resp.results[0]
    except ApiException as e:
        log.warning("HubSpot search failed for %s: %s", email, e)
    return None


def _build_properties(sender: Sender, existing: dict | None) -> dict[str, str]:
    """Build the property dict to send to HubSpot (only non-empty / missing fields)."""
    props: dict[str, str] = {}

    def _set_if_missing(key: str, value: str) -> None:
        if not value:
            return
        current = (existing.properties if existing else {}).get(key, "") or ""
        if not current:
            props[key] = value

    if existing is None:
        # Always set all fields on create
        if sender.firstname:
            props["firstname"] = sender.firstname
        if sender.lastname:
            props["lastname"] = sender.lastname
        if sender.company:
            props["company"] = sender.company
        props["leadsource"] = CONTACT_SOURCE
        props["hs_lead_status"] = "NEW"
    else:
        # Only fill in missing fields on update
        _set_if_missing("firstname", sender.firstname)
        _set_if_missing("lastname", sender.lastname)
        _set_if_missing("company", sender.company)
        _set_if_missing("leadsource", CONTACT_SOURCE)

    return props


def upsert_contact(client: hubspot.Client, sender: Sender) -> SyncResult:
    """Create or update a HubSpot contact. Returns a SyncResult."""
    existing = _find_contact_by_email(client, sender.email)
    props = _build_properties(sender, existing)

    if existing is None:
        # Create
        try:
            obj = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties={"email": sender.email, **props}
                )
            )
            _add_note(client, obj.id, sender)
            log.info("CREATED  %-40s  id=%s", sender.email, obj.id)
            return SyncResult(email=sender.email, status="created", contact_id=obj.id)
        except ApiException as e:
            log.error("Create failed for %s: %s", sender.email, e)
            return SyncResult(email=sender.email, status="error", reason=str(e))
    else:
        contact_id = existing.id
        if not props:
            log.info("IGNORED  %-40s  id=%s  (no new fields)", sender.email, contact_id)
            return SyncResult(email=sender.email, status="ignored", contact_id=contact_id,
                              reason="no missing fields")
        # Update
        try:
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            log.info("UPDATED  %-40s  id=%s  fields=%s", sender.email, contact_id,
                     list(props.keys()))
            return SyncResult(email=sender.email, status="updated", contact_id=contact_id)
        except ApiException as e:
            log.error("Update failed for %s: %s", sender.email, e)
            return SyncResult(email=sender.email, status="error", reason=str(e))


def _add_note(client: hubspot.Client, contact_id: str, sender: Sender) -> None:
    """Attach a CRM note marking the contact as inbound via Gmail."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"Contatto acquisito via {INBOUND_TAG}.\n"
        f"Dominio: {sender.domain}\n"
        f"Azienda rilevata: {sender.company or 'n/d'}\n"
        f"Tag: {INBOUND_TAG}"
    )
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": body,
                    "hs_timestamp": str(now_ms),
                }
            )
        )
        # Associate note → contact
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as e:
        log.debug("Could not create note for contact %s: %s", contact_id, e)


# ---------------------------------------------------------------------------
# Sync loop
# ---------------------------------------------------------------------------

def sync_once(gmail_service, hs_client: hubspot.Client,
              query: str = "in:inbox newer_than:1d") -> list[SyncResult]:
    log.info("Fetching Gmail threads with query: %r", query)
    senders = fetch_inbox_senders(gmail_service, query)
    log.info("Found %d unique senders to evaluate", len(senders))

    results: list[SyncResult] = []
    for sender in senders:
        result = upsert_contact(hs_client, sender)
        results.append(result)

    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    ignored = sum(1 for r in results if r.status == "ignored")
    log.info("Sync complete — created=%d  updated=%d  ignored=%d", created, updated, ignored)
    return results


def print_report(results: list[SyncResult]) -> None:
    print("\n" + "=" * 70)
    print(f"{'STATUS':<10} {'EMAIL':<40} {'HUBSPOT ID'}")
    print("-" * 70)
    for r in results:
        print(f"{r.status.upper():<10} {r.email:<40} {r.contact_id or '—'}")
    print("=" * 70 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true",
                        help="Run a single sync pass then exit")
    parser.add_argument("--query", default="in:inbox newer_than:1d",
                        help="Gmail search query (default: in:inbox newer_than:1d)")
    args = parser.parse_args()

    gmail_service = get_gmail_service()
    hs_client = get_hubspot_client()

    if args.once:
        results = sync_once(gmail_service, hs_client, args.query)
        print_report(results)
        return

    log.info("Starting continuous sync (interval=%ds, query=%r)", SYNC_INTERVAL, args.query)
    while True:
        try:
            results = sync_once(gmail_service, hs_client, args.query)
            print_report(results)
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            sys.exit(0)
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)
        log.info("Sleeping %d seconds…", SYNC_INTERVAL)
        time.sleep(SYNC_INTERVAL)


if __name__ == "__main__":
    main()
