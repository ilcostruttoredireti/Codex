#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Scans Gmail inbox for incoming emails and syncs sender contacts to HubSpot:
- New contact  → creates with all available fields
- Existing contact → fills in any missing fields
- Deduplicates by email address (email is the unique key)
- Tags every contact with source = 'Inbound Gmail'

Usage:
    python sync.py                  # last 1 day
    python sync.py --days 7         # last 7 days
    python sync.py --days 1 --dry-run

Environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN   HubSpot private-app access token
    OWN_EMAIL_1            Primary Gmail address to skip as sender
    OWN_EMAIL_2            Secondary address to skip (optional)
"""

import argparse
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_FILE = Path("token.json")
GMAIL_CREDENTIALS_FILE = Path("credentials.json")

HUBSPOT_ACCESS_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]

_OWN_EMAILS = {
    e.lower()
    for e in [
        os.environ.get("OWN_EMAIL_1", ""),
        os.environ.get("OWN_EMAIL_2", ""),
    ]
    if e
}

# Domains that only produce automated / bounce messages
_SKIP_DOMAINS = {
    "googlemail.com",
    "google.com",
    "facebookmail.com",
    "notifications.google.com",
    "accounts.google.com",
}

# Domains that are personal freemail (no company inference)
_FREEMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "yahoo.it",
    "hotmail.com",
    "hotmail.it",
    "outlook.com",
    "outlook.it",
    "libero.it",
    "virgilio.it",
    "tiscali.it",
    "icloud.com",
    "me.com",
    "live.it",
    "live.com",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def _gmail_service():
    """Authenticate (OAuth2 + token cache) and return Gmail API service."""
    creds: Optional[Credentials] = None

    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"{GMAIL_CREDENTIALS_FILE} not found. "
                    "Download it from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _parse_from_header(from_header: str) -> dict:
    """Return dict with email, first_name, last_name from a raw From header."""
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    first_name = last_name = ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    domain = email.split("@")[1] if "@" in email else ""
    company = ""
    if domain and domain not in _FREEMAIL_DOMAINS:
        # e.g. "rec-media.it" → "Rec Media"
        slug = domain.split(".")[0]  # take leftmost label
        company = re.sub(r"[-_]", " ", slug).title()

    return {
        "email": email,
        "display_name": display_name,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
    }


def _should_skip(email: str) -> bool:
    if not email or "@" not in email:
        return True
    domain = email.split("@")[1].lower()
    if domain in _SKIP_DOMAINS:
        return True
    local = email.split("@")[0].lower()
    if local in ("mailer-daemon", "postmaster", "noreply", "no-reply", "bounce"):
        return True
    if email in _OWN_EMAILS:
        return True
    return False


def fetch_senders(days: int = 1) -> list[dict]:
    """
    Return a deduplicated list of sender dicts from Gmail inbox
    for the past *days* days, excluding automated / self-sent messages.
    """
    service = _gmail_service()
    query = f"in:inbox -from:me newer_than:{days}d"
    senders: dict[str, dict] = {}
    page_token = None

    log.info("Fetching Gmail inbox  query='%s'", query)

    while True:
        resp = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=500,
            **(dict(pageToken=page_token) if page_token else {}),
        ).execute()

        for msg_stub in resp.get("messages", []):
            try:
                msg = service.users().messages().get(
                    userId="me",
                    id=msg_stub["id"],
                    format="metadata",
                    metadataHeaders=["From"],
                ).execute()
                headers = {
                    h["name"]: h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                from_raw = headers.get("From", "")
                if not from_raw:
                    continue
                info = _parse_from_header(from_raw)
                if _should_skip(info["email"]):
                    continue
                senders.setdefault(info["email"], info)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipped message %s: %s", msg_stub["id"], exc)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    log.info("Unique external senders found: %d", len(senders))
    return list(senders.values())


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

def _hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def _find_contact(client, email: str):
    """Return the existing HubSpot contact for *email*, or None."""
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(property_name="email", operator="EQ", value=email)
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source_data_2"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        return resp.results[0] if resp.total > 0 else None
    except ApiException as exc:
        log.error("HubSpot search error (%s): %s", email, exc)
        return None


def _create_contact(client, sender: dict, dry_run: bool = False) -> Optional[str]:
    props = {
        "email": sender["email"],
        "hs_analytics_source_data_2": "Inbound Gmail",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if sender["company"]:
        props["company"] = sender["company"]

    if dry_run:
        log.info("[DRY-RUN] Would create: %s", props)
        return "dry-run"

    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return contact.id
    except ApiException as exc:
        log.error("Create contact failed (%s): %s", sender["email"], exc)
        return None


def _update_contact(
    client, contact_id: str, sender: dict, existing, dry_run: bool = False
) -> bool:
    """Fill in any fields that are currently empty on the existing contact."""
    ep = existing.properties
    updates = {}

    if not ep.get("firstname") and sender["first_name"]:
        updates["firstname"] = sender["first_name"]
    if not ep.get("lastname") and sender["last_name"]:
        updates["lastname"] = sender["last_name"]
    if not ep.get("company") and sender["company"]:
        updates["company"] = sender["company"]
    # Always ensure the Inbound Gmail tag is present
    if not ep.get("hs_analytics_source_data_2"):
        updates["hs_analytics_source_data_2"] = "Inbound Gmail"

    if not updates:
        return False

    if dry_run:
        log.info("[DRY-RUN] Would update %s: %s", contact_id, updates)
        return True

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        log.error("Update contact failed (%s): %s", contact_id, exc)
        return False


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(days: int = 1, dry_run: bool = False) -> list[dict]:
    """Run the full Gmail → HubSpot sync. Returns a list of result dicts."""
    log.info("=== Gmail → HubSpot sync  days=%d  dry_run=%s ===", days, dry_run)

    senders = fetch_senders(days=days)
    client = _hs_client()
    results = []

    for sender in senders:
        email = sender["email"]
        existing = _find_contact(client, email)

        if existing:
            updated = _update_contact(client, existing.id, sender, existing, dry_run)
            status = "Aggiornato" if updated else "Ignorato"
            contact_id = existing.id
        else:
            contact_id = _create_contact(client, sender, dry_run)
            status = "Creato" if contact_id else "Errore"

        results.append({"status": status, "email": email, "hubspot_id": contact_id})
        log.info("[%-10s]  %-45s  ID=%s", status, email, contact_id)

    # Summary
    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("Creato", "Aggiornato", "Ignorato", "Errore")}
    log.info(
        "DONE — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
        counts["Creato"], counts["Aggiornato"], counts["Ignorato"], counts["Errore"],
    )
    return results


def _print_table(results: list[dict]) -> None:
    print(f"\n{'Stato':<12} {'Email':<48} {'HubSpot ID'}")
    print("-" * 78)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<48} {r['hubspot_id'] or 'N/A'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--days", type=int, default=1,
        help="How many days of inbox to scan (default: 1)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview changes without writing to HubSpot",
    )
    args = parser.parse_args()

    output = sync(days=args.days, dry_run=args.dry_run)
    _print_table(output)
