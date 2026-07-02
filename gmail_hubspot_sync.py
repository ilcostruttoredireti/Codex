#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.

Usage:
    python gmail_hubspot_sync.py [--hours N]

Environment variables required:
    HUBSPOT_ACCESS_TOKEN   - HubSpot Private App token
    GMAIL_CREDENTIALS_PATH - Path to Gmail OAuth token file (default: gmail_token.json)
    SYNC_HOURS_BACK        - Hours of email history to scan (default: 1)
"""

import os
import re
import json
import logging
import argparse
from datetime import datetime, timedelta
from email.utils import parseaddr
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"

# Patterns that indicate automated / no-reply senders
SKIP_PATTERNS = re.compile(
    r"^(no[-_]?reply|noreply|mailer-daemon|postmaster|do[-_]?not[-_]?reply"
    r"|notifications?|automated|bounce|bounces|deamon|autorespond"
    r"|donotreply|auto-confirm|confirma[-_]ordine|invoicing|alerts?"
    r"|payments?[-_]noreply|admanager[-_]noreply|noreply[-_]google"
    r"|close_friend_updates|sellersupport|system[@])",
    re.IGNORECASE,
)

SKIP_DOMAINS = {
    "amazonses.com", "sendgrid.net", "mailchimp.com", "facebookmail.com",
    "bounce.amazonses.com", "em.sparkpostmail.com",
}


def should_skip(email: str) -> bool:
    local = email.split("@")[0] if "@" in email else email
    domain = email.split("@")[-1] if "@" in email else ""
    if SKIP_PATTERNS.match(local):
        return True
    if any(d in domain for d in SKIP_DOMAINS):
        return True
    return False


def parse_sender(raw: str) -> tuple[str, str, str]:
    """Return (display_name, email_address, domain)."""
    name, addr = parseaddr(raw)
    if not addr and "@" in raw:
        addr = raw.strip()
    addr = addr.lower().strip()
    domain = addr.split("@")[-1] if "@" in addr else ""
    return name.strip(), addr, domain


def company_from_domain(domain: str) -> str:
    """Best-effort company name from domain."""
    # Drop known mail subdomains
    for prefix in ("mail.", "news.", "info.", "updates.", "m.", "em.", "marketing."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    parts = domain.split(".")
    name = parts[0] if parts else domain
    return name.replace("-", " ").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def build_gmail_service(credentials_path: str):
    creds = Credentials.from_authorized_user_file(credentials_path, GMAIL_SCOPES)
    return build("gmail", "v1", credentials=creds)


def fetch_senders(service, hours_back: int) -> list[dict]:
    """Return unique senders from inbox emails in the last N hours."""
    query = f"in:inbox -from:me newer_than:{hours_back}h"
    response = service.users().messages().list(
        userId="me", q=query, maxResults=200
    ).execute()

    messages = response.get("messages", [])
    seen: set[str] = set()
    results: list[dict] = []

    for msg in messages:
        detail = service.users().messages().get(
            userId="me",
            id=msg["id"],
            format="metadata",
            metadataHeaders=["From", "Subject", "Date"],
        ).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        raw_from = headers.get("From", "")
        _, addr, _ = parse_sender(raw_from)
        if addr and addr not in seen:
            seen.add(addr)
            results.append({
                "id": msg["id"],
                "from": raw_from,
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
            })

    return results


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

class HubSpotClient:
    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def search_contact(self, email: str) -> Optional[dict]:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{"filters": [
                {"propertyName": "email", "operator": "EQ", "value": email}
            ]}],
            "properties": ["email", "firstname", "lastname", "company", "leadsource"],
        }
        resp = self._session.post(url, json=payload)
        resp.raise_for_status()
        hits = resp.json().get("results", [])
        return hits[0] if hits else None

    def create_contact(self, properties: dict) -> str:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
        resp = self._session.post(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()["id"]

    def update_contact(self, contact_id: str, properties: dict) -> None:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
        resp = self._session.patch(url, json={"properties": properties})
        resp.raise_for_status()

    def add_note(self, contact_id: str, body: str) -> None:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(datetime.utcnow().timestamp() * 1000)),
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": 202,
                }],
            }],
        }
        try:
            self._session.post(url, json=payload).raise_for_status()
        except Exception as exc:
            logger.warning("Note creation failed for %s: %s", contact_id, exc)


# ---------------------------------------------------------------------------
# Core sync logic
# ---------------------------------------------------------------------------

def process_email(msg: dict, hs: HubSpotClient) -> dict:
    name, addr, domain = parse_sender(msg["from"])
    subject = msg.get("subject", "")

    if not addr:
        return {"status": "Ignorato", "email": addr, "contact_id": None, "reason": "no-address"}

    if should_skip(addr):
        return {"status": "Ignorato", "email": addr, "contact_id": None, "reason": "automated"}

    parts = name.split() if name else []
    firstname = parts[0] if parts else ""
    lastname = " ".join(parts[1:]) if len(parts) > 1 else ""
    company = company_from_domain(domain)

    try:
        existing = hs.search_contact(addr)

        if existing:
            cid = existing["id"]
            props = existing.get("properties", {})
            updates: dict = {}
            if not props.get("firstname") and firstname:
                updates["firstname"] = firstname
            if not props.get("lastname") and lastname:
                updates["lastname"] = lastname
            if not props.get("company") and company:
                updates["company"] = company
            if updates:
                hs.update_contact(cid, updates)
            hs.add_note(cid, f"📧 Email ricevuta da {addr}\nOggetto: {subject}\nFonte: Inbound Gmail")
            return {"status": "Aggiornato", "email": addr, "contact_id": cid}

        new_props = {
            "email": addr,
            "leadsource": "Gmail",
            "hs_lead_status": "NEW",
        }
        if firstname:
            new_props["firstname"] = firstname
        if lastname:
            new_props["lastname"] = lastname
        if company:
            new_props["company"] = company

        cid = hs.create_contact(new_props)
        hs.add_note(cid, f"📧 Primo contatto via email\nOggetto: {subject}\nFonte: Inbound Gmail\nTag: Inbound Gmail")
        return {"status": "Creato", "email": addr, "contact_id": cid}

    except requests.HTTPError as exc:
        logger.error("HubSpot error for %s: %s", addr, exc.response.text)
        return {"status": "Errore", "email": addr, "contact_id": None, "error": str(exc)}
    except Exception as exc:
        logger.error("Unexpected error for %s: %s", addr, exc)
        return {"status": "Errore", "email": addr, "contact_id": None, "error": str(exc)}


def run_sync(hours_back: int, hubspot_token: str, gmail_creds_path: str) -> list[dict]:
    logger.info("Starting Gmail → HubSpot sync (last %d hour(s))", hours_back)
    gmail = build_gmail_service(gmail_creds_path)
    hs = HubSpotClient(hubspot_token)

    senders = fetch_senders(gmail, hours_back)
    logger.info("Found %d unique senders to process", len(senders))

    results = []
    for msg in senders:
        result = process_email(msg, hs)
        results.append(result)
        if result["status"] != "Ignorato":
            logger.info(
                "[%s] %s → ID: %s",
                result["status"], result["email"], result.get("contact_id", "-")
            )

    created = sum(1 for r in results if r["status"] == "Creato")
    updated = sum(1 for r in results if r["status"] == "Aggiornato")
    skipped = sum(1 for r in results if r["status"] == "Ignorato")
    errors  = sum(1 for r in results if r["status"] == "Errore")

    logger.info(
        "Done — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
        created, updated, skipped, errors,
    )
    return results


def print_report(results: list[dict]) -> None:
    print("\n" + "=" * 72)
    print("  GMAIL → HUBSPOT CONTACT SYNC  —  Report")
    print("=" * 72)
    print(f"{'Status':<12} {'Email':<42} {'HubSpot ID'}")
    print("-" * 72)
    for r in results:
        if r["status"] != "Ignorato":
            print(f"{r['status']:<12} {r['email']:<42} {r.get('contact_id') or '-'}")
    skipped = [r for r in results if r["status"] == "Ignorato"]
    if skipped:
        print(f"\n  (+ {len(skipped)} mittenti automatici ignorati)")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot CRM")
    parser.add_argument("--hours", type=int, default=None, help="Hours of email to scan")
    args = parser.parse_args()

    token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    creds_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "gmail_token.json")
    hours = args.hours or int(os.environ.get("SYNC_HOURS_BACK", "1"))

    if not token:
        raise SystemExit("ERROR: HUBSPOT_ACCESS_TOKEN environment variable not set.")
    if not os.path.exists(creds_path):
        raise SystemExit(f"ERROR: Gmail credentials file not found: {creds_path}")

    results = run_sync(hours_back=hours, hubspot_token=token, gmail_creds_path=creds_path)
    print_report(results)
