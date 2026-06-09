#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and syncs senders as contacts in HubSpot.

Usage:
    python gmail_hubspot_sync.py [--once]

Environment variables:
    HUBSPOT_ACCESS_TOKEN   HubSpot private-app access token (required)
    GOOGLE_CREDENTIALS_FILE  Path to Google OAuth2 client secrets JSON (default: credentials.json)
    GOOGLE_TOKEN_FILE        Path to store/load OAuth2 token (default: token.json)
    POLL_INTERVAL            Seconds between polls (default: 60)
"""

import argparse
import logging
import os
import sys
import time
from email.utils import parseaddr
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PROCESSED_LABEL_NAME = "HubSpot-Synced"
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))

# Domains treated as personal/free → no company inference
FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
    "live.com", "icloud.com", "me.com", "mac.com",
    "libero.it", "tin.it", "virgilio.it", "tiscali.it",
    "alice.it", "email.it", "inwind.it",
}

# Sender prefixes / addresses to skip entirely
SKIP_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notification", "notifications", "alert", "alerts",
    "support+", "info+", "newsletter", "analytics-noreply",
    "posta-certificata", "daemon", "abuse",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail ─────────────────────────────────────────────────────────────────────

def gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                log.error("Google credentials file not found: %s", CREDENTIALS_FILE)
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        Path(TOKEN_FILE).write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def get_or_create_label(svc, name: str) -> str:
    labels = svc.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = svc.users().labels().create(
        userId="me",
        body={
            "name": name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    ).execute()
    log.info("Created Gmail label: %s (%s)", name, created["id"])
    return created["id"]


def fetch_unprocessed(svc, processed_label_id: str) -> list[dict]:
    query = f"in:inbox -label:{PROCESSED_LABEL_NAME} -from:me -category:promotions -category:social"
    result = svc.users().messages().list(
        userId="me", q=query, maxResults=100
    ).execute()
    return result.get("messages", [])


def get_sender(svc, msg_id: str) -> dict:
    msg = svc.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "Subject", "Date"],
    ).execute()
    hdrs = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    name, email = parseaddr(hdrs.get("From", ""))
    return {
        "message_id": msg_id,
        "name": name.strip(),
        "email": email.strip().lower(),
        "subject": hdrs.get("Subject", ""),
        "date": hdrs.get("Date", ""),
    }


def mark_processed(svc, msg_id: str, label_id: str):
    svc.users().messages().modify(
        userId="me",
        id=msg_id,
        body={"addLabelIds": [label_id]},
    ).execute()


# ── Contact extraction ────────────────────────────────────────────────────────

def should_skip(email: str) -> bool:
    if not email or "@" not in email:
        return True
    local, domain = email.lower().split("@", 1)
    # Skip if local part starts with a known automated prefix
    if any(local.startswith(p) for p in SKIP_PREFIXES):
        return True
    # Skip if "noreply" appears anywhere in the address (e.g. analytics-noreply@google.com)
    if "noreply" in email.lower() or "no-reply" in email.lower():
        return True
    # Skip known automated/delivery domains
    if domain in {"googlemail.com"} and local == "mailer-daemon":
        return True
    return False


def parse_contact(sender: dict) -> dict:
    email = sender["email"]
    name = sender["name"]
    domain = email.split("@")[1] if "@" in email else ""

    parts = name.split(None, 1) if name else []
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""

    company = ""
    if domain and domain.lower() not in FREE_DOMAINS:
        raw = domain.rsplit(".", 2)
        company_slug = raw[-2] if len(raw) >= 2 else raw[0]
        company = company_slug.replace("-", " ").replace("_", " ").title()

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


# ── HubSpot ───────────────────────────────────────────────────────────────────

def hs_client():
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact(client, email: str):
    """Return existing HubSpot contact or None."""
    f = Filter(property_name="email", operator="EQ", value=email)
    req = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[f])],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
    )
    try:
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        return resp.results[0] if resp.results else None
    except ApiException as exc:
        log.warning("HubSpot search failed for %s: %s", email, exc.reason)
        return None


def create_contact(client, parts: dict) -> str:
    props = {
        "email": parts["email"],
        "hs_analytics_source": "OTHER",
        "hs_analytics_source_data_1": "Inbound Gmail",
    }
    if parts["firstname"]:
        props["firstname"] = parts["firstname"]
    if parts["lastname"]:
        props["lastname"] = parts["lastname"]
    if parts["company"]:
        props["company"] = parts["company"]

    result = client.crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=props, associations=[]
        )
    )
    return result.id


def update_contact(client, contact_id: str, parts: dict, existing) -> bool:
    ep = existing.properties or {}
    updates = {}

    if parts["firstname"] and not ep.get("firstname"):
        updates["firstname"] = parts["firstname"]
    if parts["lastname"] and not ep.get("lastname"):
        updates["lastname"] = parts["lastname"]
    if parts["company"] and not ep.get("company"):
        updates["company"] = parts["company"]

    if updates:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    return False


def add_email_note(client, contact_id: str, sender: dict):
    """Create a HubSpot Note logging the inbound email."""
    body = (
        f"📧 Email ricevuta via Gmail\n"
        f"Da: {sender['name']} <{sender['email']}>\n"
        f"Oggetto: {sender['subject']}\n"
        f"Data: {sender['date']}\n"
        f"Tag: Inbound Gmail"
    )
    try:
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
        note = NoteCreate(
            properties={
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            associations=[
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        )
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
    except Exception as exc:
        log.debug("Note creation skipped: %s", exc)


# ── Sync loop ─────────────────────────────────────────────────────────────────

def run_batch(gmail_svc, hs, label_id: str, add_notes: bool = True) -> list[dict]:
    messages = fetch_unprocessed(gmail_svc, label_id)
    if not messages:
        return []

    log.info("Found %d unprocessed emails", len(messages))
    results = []

    for msg_ref in messages:
        try:
            sender = get_sender(gmail_svc, msg_ref["id"])
            email = sender["email"]

            if not email or should_skip(email):
                mark_processed(gmail_svc, msg_ref["id"], label_id)
                results.append({"stato": "Ignorato", "email": email or "(vuoto)", "hubspot_id": None})
                log.info("%-35s → Ignorato (automated/system)", email)
                continue

            parts = parse_contact(sender)
            existing = find_contact(hs, email)

            if existing:
                updated = update_contact(hs, existing.id, parts, existing)
                stato = "Aggiornato" if updated else "Ignorato (già completo)"
                hubspot_id = existing.id
                if updated and add_notes:
                    add_email_note(hs, hubspot_id, sender)
            else:
                hubspot_id = create_contact(hs, parts)
                stato = "Creato"
                if add_notes:
                    add_email_note(hs, hubspot_id, sender)

            mark_processed(gmail_svc, msg_ref["id"], label_id)
            results.append({"stato": stato, "email": email, "hubspot_id": hubspot_id})
            log.info("%-35s → %-25s | HubSpot ID: %s", email, stato, hubspot_id)

        except HttpError as exc:
            log.error("Gmail error on msg %s: %s", msg_ref["id"], exc)
        except ApiException as exc:
            log.error("HubSpot error: %s", exc.reason)

    return results


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once", action="store_true", help="Run a single batch then exit"
    )
    parser.add_argument(
        "--no-notes", action="store_true", help="Skip creating HubSpot notes"
    )
    args = parser.parse_args()

    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN is not set")
        sys.exit(1)

    gmail_svc = gmail_service()
    hs = hs_client()
    label_id = get_or_create_label(gmail_svc, PROCESSED_LABEL_NAME)

    log.info("Gmail → HubSpot sync started | poll interval: %ds", POLL_INTERVAL)

    while True:
        try:
            results = run_batch(gmail_svc, hs, label_id, add_notes=not args.no_notes)
            if results:
                created = sum(1 for r in results if r["stato"] == "Creato")
                updated = sum(1 for r in results if r["stato"] == "Aggiornato")
                skipped = len(results) - created - updated
                log.info("Batch done → Creati: %d | Aggiornati: %d | Ignorati: %d",
                         created, updated, skipped)
        except KeyboardInterrupt:
            log.info("Interrupted by user")
            break
        except Exception as exc:
            log.error("Unexpected error: %s", exc, exc_info=True)

        if args.once:
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
