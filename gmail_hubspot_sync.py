#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail messages and creates/updates HubSpot contacts.

Required env vars:
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN
  HUBSPOT_ACCESS_TOKEN
Optional:
  LAST_CHECKED_TIMESTAMP_FILE  (path to a file storing the last run epoch, default: .last_checked)
"""

import os
import re
import json
import time
import email.utils
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"
LEAD_SOURCE_VALUE = "Gmail"          # stored in a custom text property
CUSTOM_PROP_SOURCE = "fonte_contatto"  # custom HubSpot text property (create once in settings)
CUSTOM_PROP_TAG    = "inbound_tag"     # custom HubSpot text property
IGNORED_DOMAINS = {
    "facebookmail.com", "notifications.google.com", "bounce.linkedin.com",
    "mail.instagram.com", "twitter.com", "accounts.google.com",
}
TS_FILE = os.getenv("LAST_CHECKED_TIMESTAMP_FILE", ".last_checked")


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def _build_gmail_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GMAIL_SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _load_last_timestamp() -> int:
    """Return epoch seconds of the last successful run, or 24 h ago."""
    try:
        with open(TS_FILE) as f:
            return int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return int(time.time()) - 86400


def _save_timestamp(ts: int) -> None:
    with open(TS_FILE, "w") as f:
        f.write(str(ts))


def fetch_new_inbox_messages(service, since_ts: int) -> list[dict]:
    """Return metadata-only message list newer than since_ts."""
    query = f"in:inbox -from:me after:{since_ts}"
    messages = []
    page_token = None
    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 500}
        if page_token:
            kwargs["pageToken"] = page_token
        result = service.users().messages().list(**kwargs).execute()
        messages.extend(result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return messages


def get_sender(service, msg_id: str) -> Optional[tuple[str, str, str]]:
    """
    Fetch a message header and return (email, first_name, last_name).
    Returns None if sender should be ignored.
    """
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Date"],
    ).execute()
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    raw_from = headers.get("From", "")
    if not raw_from:
        return None
    name, addr = email.utils.parseaddr(raw_from)
    addr = addr.lower().strip()
    if not addr or "@" not in addr:
        return None
    domain = addr.split("@")[1]
    if domain in IGNORED_DOMAINS:
        return None

    first, last = _split_name(name)
    return addr, first, last


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return full_name, ""


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    token = os.environ["HUBSPOT_ACCESS_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def find_contact_by_email(email_addr: str) -> Optional[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [{"filters": [
            {"propertyName": "email", "operator": "EQ", "value": email_addr}
        ]}],
        "properties": ["email", "firstname", "lastname", "company",
                        CUSTOM_PROP_SOURCE, CUSTOM_PROP_TAG],
        "limit": 1,
    }
    r = requests.post(url, json=body, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def _domain_to_company(email_addr: str) -> str:
    """Best-effort company name from email domain."""
    domain = email_addr.split("@")[1]
    # Skip generic providers
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
               "libero.it", "virgilio.it", "tiscali.it", "icloud.com"}
    if domain in generic:
        return ""
    # Strip common TLDs and capitalise
    name = re.sub(r"\.(com|it|eu|org|net|gov|edu)$", "", domain, flags=re.I)
    name = re.sub(r"\.", " ", name)
    return name.title()


def create_contact(email_addr: str, first: str, last: str) -> dict:
    company = _domain_to_company(email_addr)
    props = {
        "email": email_addr,
        "firstname": first,
        "lastname": last,
        CUSTOM_PROP_SOURCE: LEAD_SOURCE_VALUE,
        CUSTOM_PROP_TAG: "Inbound Gmail",
    }
    if company:
        props["company"] = company
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    r = requests.post(url, json={"properties": props}, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def update_contact(contact_id: str, missing: dict) -> dict:
    if not missing:
        return {}
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = requests.patch(url, json={"properties": missing}, headers=_hs_headers(), timeout=15)
    r.raise_for_status()
    return r.json()


def log_email_activity(contact_id: str, email_addr: str) -> None:
    """Create a logged inbound email engagement on the contact."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/emails"
    body = {
        "properties": {
            "hs_email_direction": "INCOMING_EMAIL",
            "hs_email_subject": "Inbound Gmail – sincronizzato automaticamente",
            "hs_email_status": "RECEIVED",
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [{"to": {"id": contact_id},
                          "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                     "associationTypeId": 198}]}],
    }
    try:
        r = requests.post(url, json=body, headers=_hs_headers(), timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.warning("Could not log email activity for %s: %s", email_addr, exc)


# ── Sync logic ────────────────────────────────────────────────────────────────

def process_message(service, msg_id: str) -> dict:
    """
    Process a single Gmail message.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "hs_id": ...}
    """
    try:
        sender = get_sender(service, msg_id)
    except Exception as exc:
        log.warning("Could not fetch message %s: %s", msg_id, exc)
        return {"status": "Ignorato", "email": "—", "hs_id": "—"}

    if sender is None:
        return {"status": "Ignorato", "email": "—", "hs_id": "—"}

    email_addr, first, last = sender
    existing = find_contact_by_email(email_addr)

    if existing is None:
        contact = create_contact(email_addr, first, last)
        hs_id = contact["id"]
        log_email_activity(hs_id, email_addr)
        log.info("Creato  | %s | %s", email_addr, hs_id)
        return {"status": "Creato", "email": email_addr, "hs_id": hs_id}

    hs_id = existing["id"]
    props = existing.get("properties", {})
    updates = {}

    # Fill missing fields
    if not props.get("firstname") and first:
        updates["firstname"] = first
    if not props.get("lastname") and last:
        updates["lastname"] = last
    if not props.get("company"):
        company = _domain_to_company(email_addr)
        if company:
            updates["company"] = company
    if not props.get(CUSTOM_PROP_SOURCE):
        updates[CUSTOM_PROP_SOURCE] = LEAD_SOURCE_VALUE
    if not props.get(CUSTOM_PROP_TAG):
        updates[CUSTOM_PROP_TAG] = "Inbound Gmail"

    if updates:
        update_contact(hs_id, updates)
        log.info("Aggiornato | %s | %s (fields: %s)", email_addr, hs_id, list(updates))
    else:
        log.info("Già completo | %s | %s", email_addr, hs_id)

    return {"status": "Aggiornato", "email": email_addr, "hs_id": hs_id}


def run_sync() -> list[dict]:
    service = _build_gmail_service()
    since_ts = _load_last_timestamp()
    now_ts = int(time.time())

    log.info("Cerco email successive a %s", datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat())
    messages = fetch_new_inbox_messages(service, since_ts)
    log.info("Trovati %d messaggi da processare", len(messages))

    seen_emails: set[str] = set()
    results = []

    for msg in messages:
        result = process_message(service, msg["id"])
        email_addr = result["email"]
        if email_addr in seen_emails:
            continue
        seen_emails.add(email_addr)
        results.append(result)

    _save_timestamp(now_ts)

    print("\n=== Riepilogo sincronizzazione Gmail → HubSpot ===")
    print(f"{'Stato':<12} {'Email':<45} {'ID HubSpot'}")
    print("-" * 75)
    for r in results:
        print(f"{r['status']:<12} {r['email']:<45} {r['hs_id']}")

    creati    = sum(1 for r in results if r["status"] == "Creato")
    aggiornati = sum(1 for r in results if r["status"] == "Aggiornato")
    ignorati   = sum(1 for r in results if r["status"] == "Ignorato")
    print(f"\nCreati: {creati}  Aggiornati: {aggiornati}  Ignorati: {ignorati}")
    return results


if __name__ == "__main__":
    run_sync()
