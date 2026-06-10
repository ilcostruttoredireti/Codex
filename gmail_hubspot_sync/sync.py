#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and syncs senders as HubSpot contacts.

Usage:
    python sync.py               # run once (process unread inbox emails)
    python sync.py --watch       # continuous polling mode (60s interval)
    python sync.py --since 7d    # process emails from last 7 days
"""

import os
import re
import sys
import time
import json
import argparse
import logging
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr
from typing import Optional

# ── Gmail SDK ──────────────────────────────────────────────────────────────────
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── HubSpot SDK ────────────────────────────────────────────────────────────────
import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
)
from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput

# ── Config ─────────────────────────────────────────────────────────────────────
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
]
TOKEN_FILE = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
HUBSPOT_API_KEY = os.environ.get("HUBSPOT_API_KEY", "")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))

# Domains / addresses to skip (automated senders)
SKIP_DOMAINS = {
    "googlemail.com", "google.com", "facebookmail.com", "facebook.com",
    "linkedin.com", "twitter.com", "instagram.com", "notifications.google.com",
    "bounce.gmail.com",
}
SKIP_PREFIXES = ("mailer-daemon", "noreply", "no-reply", "do-not-reply",
                 "postmaster", "notifications", "bounce", "alert", "support")

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"
PROCESSED_LABEL = "HS_Synced"        # Gmail label added after processing

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ── Gmail auth ─────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


# ── HubSpot client ─────────────────────────────────────────────────────────────

def get_hubspot_client() -> hubspot.Client:
    if not HUBSPOT_API_KEY:
        raise EnvironmentError("HUBSPOT_API_KEY environment variable is not set.")
    return hubspot.Client.create(access_token=HUBSPOT_API_KEY)


# ── Email parsing ──────────────────────────────────────────────────────────────

def should_skip(email_addr: str) -> bool:
    """Return True for automated/system senders that shouldn't become contacts."""
    addr = email_addr.lower().strip()
    local, _, domain = addr.partition("@")
    if domain in SKIP_DOMAINS:
        return True
    if any(local.startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (firstname, lastname). Best-effort."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def domain_to_company(domain: str) -> str:
    """Convert email domain to a readable company name guess."""
    # strip common TLDs and subdomains
    hostname = domain.lower()
    for prefix in ("mail.", "email.", "info.", "news."):
        if hostname.startswith(prefix):
            hostname = hostname[len(prefix):]
    name = hostname.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def extract_sender(raw_from: str) -> dict:
    """Parse a raw From header into structured contact data."""
    display, addr = parseaddr(raw_from)
    addr = addr.lower().strip()
    local, _, domain = addr.partition("@")
    first, last = parse_name(display) if display else ("", "")
    company = domain_to_company(domain) if domain not in ("gmail.com", "yahoo.com",
                                                            "hotmail.com", "libero.it",
                                                            "alice.it", "tiscali.it") else ""
    return {
        "email": addr,
        "firstname": first,
        "lastname": last,
        "company": company,
        "domain": domain,
    }


# ── Gmail helpers ──────────────────────────────────────────────────────────────

def get_or_create_label(svc, name: str) -> str:
    """Return the Gmail label id for PROCESSED_LABEL, creating it if needed."""
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = svc.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelHide",
              "messageListVisibility": "hide"},
    ).execute()
    log.info("Created Gmail label '%s' (id=%s)", name, created["id"])
    return created["id"]


def fetch_inbox_messages(svc, since: Optional[datetime] = None) -> list[dict]:
    """Return inbox messages not yet tagged with PROCESSED_LABEL."""
    query = f"in:inbox -from:me -label:{PROCESSED_LABEL}"
    if since:
        ts = int(since.timestamp())
        query += f" after:{ts}"
    messages, token = [], None
    while True:
        resp = svc.users().messages().list(
            userId="me", q=query, maxResults=100, pageToken=token
        ).execute()
        messages.extend(resp.get("messages", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return messages


def get_message_from_header(svc, msg_id: str) -> Optional[str]:
    """Fetch only the From header for a message (minimal format)."""
    msg = svc.users().messages().get(userId="me", id=msg_id, format="metadata",
                                     metadataHeaders=["From"]).execute()
    for h in msg.get("payload", {}).get("headers", []):
        if h["name"].lower() == "from":
            return h["value"]
    return None


def mark_processed(svc, msg_id: str, label_id: str) -> None:
    svc.users().messages().modify(
        userId="me", id=msg_id, body={"addLabelIds": [label_id]}
    ).execute()


# ── HubSpot helpers ────────────────────────────────────────────────────────────

def find_contact(hs: hubspot.Client, email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    filt = Filter(property_name="email", operator="EQ", value=email)
    group = FilterGroup(filters=[filt])
    req = PublicObjectSearchRequest(
        filter_groups=[group],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    resp = hs.crm.contacts.search_api.do_search(public_object_search_request=req)
    if resp.results:
        return resp.results[0]
    return None


def create_contact(hs: hubspot.Client, data: dict) -> dict:
    """Create a new HubSpot contact. Returns the created contact."""
    props = {
        "email": data["email"],
        "hs_analytics_source": "OTHER_CAMPAIGNS",  # closest standard value
        "hs_analytics_source_data_1": CONTACT_SOURCE,
        "hs_analytics_source_data_2": INBOUND_TAG,
    }
    for key in ("firstname", "lastname", "company"):
        if data.get(key):
            props[key] = data[key]
    contact = hs.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props
        )
    )
    return contact


def update_contact(hs: hubspot.Client, contact_id: str, data: dict, existing: dict) -> bool:
    """Fill in any missing fields on an existing contact. Returns True if updated."""
    existing_props = existing.properties if hasattr(existing, "properties") else {}
    updates = {}
    for key in ("firstname", "lastname", "company"):
        if data.get(key) and not existing_props.get(key):
            updates[key] = data[key]
    if not updates:
        return False
    hs.crm.contacts.basic_api.update(
        contact_id=str(contact_id),
        simple_public_object_input=SimplePublicObjectInput(properties=updates),
    )
    return True


def add_gmail_note(hs: hubspot.Client, contact_id: str, sender_email: str) -> None:
    """Attach a timeline note to the contact recording the Gmail inbound event."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"📬 Email in entrata ricevuta via Gmail\n"
        f"Mittente: {sender_email}\n"
        f"Tag: {INBOUND_TAG}\n"
        f"Fonte: {CONTACT_SOURCE}\n"
        f"Data: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    note = hs.crm.objects.notes.basic_api.create(
        simple_public_object_input_for_create=NoteInput(
            properties={
                "hs_note_body": body,
                "hs_timestamp": str(now_ms),
            },
            associations=[
                {
                    "to": {"id": str(contact_id)},
                    "types": [
                        {"associationCategory": "HUBSPOT_DEFINED",
                         "associationTypeId": 202}   # note → contact
                    ],
                }
            ],
        )
    )
    return note


# ── Core sync logic ────────────────────────────────────────────────────────────

def process_messages(
    gmail_svc,
    hs_client: hubspot.Client,
    messages: list[dict],
    processed_label_id: str,
    dry_run: bool = False,
) -> list[dict]:
    """Process a list of Gmail message stubs. Returns a result log."""
    seen_emails: set[str] = set()
    results = []

    for msg in messages:
        raw_from = get_message_from_header(gmail_svc, msg["id"])
        if not raw_from:
            continue

        sender = extract_sender(raw_from)
        email = sender["email"]

        if not email or "@" not in email:
            continue
        if should_skip(email):
            log.debug("Skipping automated sender: %s", email)
            continue
        if email in seen_emails:
            # already handled in this batch
            mark_processed(gmail_svc, msg["id"], processed_label_id)
            continue
        seen_emails.add(email)

        log.info("Processing: %s", email)
        result = {"email": email, "status": None, "hubspot_id": None}

        try:
            existing = find_contact(hs_client, email)

            if existing:
                contact_id = existing.id if hasattr(existing, "id") else existing["id"]
                updated = False
                if not dry_run:
                    updated = update_contact(hs_client, contact_id, sender, existing)
                    add_gmail_note(hs_client, contact_id, email)
                result["status"] = "Aggiornato" if updated else "Ignorato (già completo)"
                result["hubspot_id"] = contact_id
                log.info("  → %s | HubSpot ID: %s", result["status"], contact_id)

            else:
                if not dry_run:
                    created = create_contact(hs_client, sender)
                    contact_id = created.id if hasattr(created, "id") else created["id"]
                    add_gmail_note(hs_client, contact_id, email)
                else:
                    contact_id = "DRY_RUN"
                result["status"] = "Creato"
                result["hubspot_id"] = contact_id
                log.info("  → Creato | HubSpot ID: %s", contact_id)

        except ApiException as exc:
            log.error("  HubSpot error for %s: %s", email, exc)
            result["status"] = f"Errore: {exc.status}"

        if not dry_run:
            mark_processed(gmail_svc, msg["id"], processed_label_id)

        results.append(result)

    return results


def print_report(results: list[dict]) -> None:
    print("\n" + "─" * 60)
    print(f"{'EMAIL':<38} {'STATO':<20} {'HUBSPOT ID'}")
    print("─" * 60)
    for r in results:
        print(f"{r['email']:<38} {r['status']:<20} {r['hubspot_id'] or '—'}")
    print("─" * 60)
    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        for k in totals:
            if r["status"] and r["status"].startswith(k):
                totals[k] += 1
    print(f"Totale: {len(results)} | " +
          " | ".join(f"{k}: {v}" for k, v in totals.items() if v))
    print()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--watch", action="store_true",
                        help="Continuous polling mode")
    parser.add_argument("--since", default=None,
                        help="Process emails newer than this (e.g. 7d, 24h, 2026-01-01)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Parse emails but do not write to HubSpot")
    args = parser.parse_args()

    since: Optional[datetime] = None
    if args.since:
        if args.since.endswith("d"):
            since = datetime.now(timezone.utc) - timedelta(days=int(args.since[:-1]))
        elif args.since.endswith("h"):
            since = datetime.now(timezone.utc) - timedelta(hours=int(args.since[:-1]))
        else:
            since = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()
    processed_label_id = get_or_create_label(gmail_svc, PROCESSED_LABEL)

    log.info("Gmail → HubSpot sync started%s", " (dry-run)" if args.dry_run else "")

    while True:
        messages = fetch_inbox_messages(gmail_svc, since=since)
        log.info("Found %d inbox messages to process", len(messages))

        if messages:
            results = process_messages(
                gmail_svc, hs_client, messages, processed_label_id, dry_run=args.dry_run
            )
            print_report(results)
        else:
            log.info("No new messages.")

        if not args.watch:
            break

        log.info("Waiting %ds before next check…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
