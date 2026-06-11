#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for inbound emails and syncs senders as HubSpot contacts.
Skips: own addresses, mailer-daemons, forwarding aliases.

Usage:
    python gmail_hubspot_sync.py [--hours 24] [--dry-run]

Environment variables required:
    GMAIL_CREDENTIALS_FILE   Path to Gmail OAuth2 credentials JSON (from Google Cloud Console)
    GMAIL_TOKEN_FILE         Path to store/load the OAuth token (auto-created on first run)
    HUBSPOT_ACCESS_TOKEN     HubSpot private app access token

Optional:
    OWN_EMAILS               Comma-separated list of your own email addresses to skip
    SYNC_LOOKBACK_HOURS      How many hours back to scan (default: 24)
"""

import os
import re
import json
import time
import argparse
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from dataclasses import dataclass, field
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Personal/generic email domains — company name is NOT derived from these
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "protonmail.com", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tin.it",
}

# Skip messages from these senders
SKIP_SENDER_PATTERNS = [
    r"mailer-daemon@",
    r"postmaster@",
    r"noreply@",
    r"no-reply@",
    r"donotreply@",
    r"bounces?@",
]

HUBSPOT_BASE = "https://api.hubapi.com"
HUBSPOT_CONTACT_SOURCE = "OFFLINE"   # closest standard value for Gmail inbound
GMAIL_TAG_LABEL = "Inbound Gmail"


@dataclass
class SenderInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    domain: str = ""
    email_subject: str = ""
    email_date: str = ""


@dataclass
class SyncResult:
    email: str
    status: str          # Creato / Aggiornato / Ignorato
    hubspot_id: Optional[str] = None
    reason: str = ""


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def get_gmail_service(credentials_file: str, token_file: str):
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_file, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def fetch_inbound_emails(service, own_emails: set[str], lookback_hours: int) -> list[SenderInfo]:
    after = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp())
    query = f"in:inbox -from:me after:{after}"

    results = service.users().messages().list(
        userId="me", q=query, maxResults=200
    ).execute()

    messages = results.get("messages", [])
    log.info(f"Found {len(messages)} inbox messages in last {lookback_hours}h")

    senders: dict[str, SenderInfo] = {}

    for msg_ref in messages:
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_ref["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
        except HttpError as e:
            log.warning(f"Could not fetch message {msg_ref['id']}: {e}")
            continue

        headers = msg.get("payload", {}).get("headers", [])
        raw_from = _header(headers, "From")
        subject  = _header(headers, "Subject")
        date     = _header(headers, "Date")

        display_name, email_addr = parseaddr(raw_from)
        email_addr = email_addr.lower().strip()

        if not email_addr or "@" not in email_addr:
            continue
        if email_addr in own_emails:
            continue
        if any(re.search(p, email_addr) for p in SKIP_SENDER_PATTERNS):
            continue
        if email_addr in senders:
            continue  # already queued

        domain = email_addr.split("@")[-1]
        firstname, lastname = _split_name(display_name, email_addr)
        company = "" if domain in PERSONAL_DOMAINS else _domain_to_company(domain)

        senders[email_addr] = SenderInfo(
            email=email_addr,
            firstname=firstname,
            lastname=lastname,
            company=company,
            domain=domain,
            email_subject=subject,
            email_date=date,
        )

    return list(senders.values())


def _split_name(display_name: str, email: str) -> tuple[str, str]:
    display_name = display_name.strip('"').strip()
    if not display_name:
        local = email.split("@")[0]
        parts = re.split(r"[._\-]", local)
        parts = [p.capitalize() for p in parts if p]
        return (parts[0] if parts else "", " ".join(parts[1:]) if len(parts) > 1 else "")
    parts = display_name.split()
    return (parts[0] if parts else "", " ".join(parts[1:]) if len(parts) > 1 else "")


def _domain_to_company(domain: str) -> str:
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ── HubSpot helpers ────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    def _get(self, path: str, **kwargs) -> dict:
        r = self.session.get(f"{HUBSPOT_BASE}{path}", **kwargs)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = self.session.post(f"{HUBSPOT_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def _patch(self, path: str, body: dict) -> dict:
        r = self.session.patch(f"{HUBSPOT_BASE}{path}", json=body)
        r.raise_for_status()
        return r.json()

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["email", "firstname", "lastname", "company",
                           "hs_analytics_source", "hs_email_domain"],
            "limit": 1,
        }
        data = self._post("/crm/v3/objects/contacts/search", body)
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, sender: SenderInfo) -> dict:
        props = _build_props(sender, existing=None)
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, sender: SenderInfo, existing: dict) -> dict:
        props = _build_props(sender, existing=existing)
        if not props:
            return existing
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    def create_note(self, contact_id: str, sender: SenderInfo) -> None:
        note_body = (
            f"[{GMAIL_TAG_LABEL}] Email inbound ricevuta\n"
            f"Da: {sender.email}\n"
            f"Oggetto: {sender.email_subject}\n"
            f"Data: {sender.email_date}\n"
            f"Fonte contatto: Gmail"
        )
        engagement = {
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {"body": note_body},
        }
        try:
            self.session.post(
                f"{HUBSPOT_BASE}/engagements/v1/engagements", json=engagement
            ).raise_for_status()
        except Exception as e:
            log.warning(f"Could not create note for {contact_id}: {e}")


def _build_props(sender: SenderInfo, existing: Optional[dict]) -> dict:
    existing_props = existing.get("properties", {}) if existing else {}
    props = {}

    def _set_if_missing(key: str, value: str):
        if value and not existing_props.get(key):
            props[key] = value

    _set_if_missing("email", sender.email)
    _set_if_missing("firstname", sender.firstname)
    _set_if_missing("lastname", sender.lastname)
    _set_if_missing("company", sender.company)
    _set_if_missing("hs_email_domain", sender.domain)

    if not existing_props.get("hs_analytics_source"):
        props["hs_analytics_source"] = HUBSPOT_CONTACT_SOURCE

    return props


# ── Main sync loop ─────────────────────────────────────────────────────────────

def sync(
    gmail_credentials: str,
    gmail_token: str,
    hubspot_token: str,
    own_emails: set[str],
    lookback_hours: int,
    dry_run: bool,
    create_notes: bool,
) -> list[SyncResult]:
    service = get_gmail_service(gmail_credentials, gmail_token)
    hs = HubSpotClient(hubspot_token)

    senders = fetch_inbound_emails(service, own_emails, lookback_hours)
    log.info(f"Unique inbound senders to process: {len(senders)}")

    results: list[SyncResult] = []

    for sender in senders:
        try:
            existing = hs.find_contact_by_email(sender.email)

            if existing:
                contact_id = existing["id"]
                if not dry_run:
                    hs.update_contact(contact_id, sender, existing)
                    if create_notes:
                        hs.create_note(contact_id, sender)
                log.info(f"[Aggiornato] {sender.email} → ID {contact_id}")
                results.append(SyncResult(email=sender.email, status="Aggiornato", hubspot_id=contact_id))
            else:
                if not dry_run:
                    created = hs.create_contact(sender)
                    contact_id = created["id"]
                    if create_notes:
                        hs.create_note(contact_id, sender)
                else:
                    contact_id = "DRY-RUN"
                log.info(f"[Creato]    {sender.email} → ID {contact_id}")
                results.append(SyncResult(email=sender.email, status="Creato", hubspot_id=contact_id))

            time.sleep(0.1)  # avoid HubSpot rate limit

        except Exception as e:
            log.error(f"[Errore]    {sender.email}: {e}")
            results.append(SyncResult(email=sender.email, status="Ignorato", reason=str(e)))

    return results


def print_summary(results: list[SyncResult]) -> None:
    print("\n" + "=" * 70)
    print(f"{'STATO':<12} {'EMAIL CONTATTO':<40} {'ID HUBSPOT'}")
    print("-" * 70)
    for r in results:
        print(f"{r.status:<12} {r.email:<40} {r.hubspot_id or '-'}")
        if r.reason:
            print(f"{'':12} ↳ {r.reason}")
    print("=" * 70)
    created  = sum(1 for r in results if r.status == "Creato")
    updated  = sum(1 for r in results if r.status == "Aggiornato")
    skipped  = sum(1 for r in results if r.status == "Ignorato")
    print(f"Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {skipped}\n")


def main():
    parser = argparse.ArgumentParser(description="Sync Gmail inbound senders to HubSpot contacts")
    parser.add_argument("--hours", type=int, default=int(os.getenv("SYNC_LOOKBACK_HOURS", 24)))
    parser.add_argument("--dry-run", action="store_true", help="Parse emails but do not write to HubSpot")
    parser.add_argument("--no-notes", action="store_true", help="Skip creating timeline notes")
    args = parser.parse_args()

    gmail_creds = os.environ["GMAIL_CREDENTIALS_FILE"]
    gmail_token = os.environ.get("GMAIL_TOKEN_FILE", "gmail_token.json")
    hs_token    = os.environ["HUBSPOT_ACCESS_TOKEN"]
    own_raw     = os.environ.get("OWN_EMAILS", "")
    own_emails  = {e.strip().lower() for e in own_raw.split(",") if e.strip()}

    if args.dry_run:
        log.info("DRY RUN — no writes to HubSpot")

    results = sync(
        gmail_credentials=gmail_creds,
        gmail_token=gmail_token,
        hubspot_token=hs_token,
        own_emails=own_emails,
        lookback_hours=args.hours,
        dry_run=args.dry_run,
        create_notes=not args.no_notes,
    )
    print_summary(results)


if __name__ == "__main__":
    main()
