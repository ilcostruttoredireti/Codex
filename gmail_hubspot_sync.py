#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.
Uses email as unique key to avoid duplicates; updates existing records.
"""

import os
import re
import json
import time
import logging
import base64
from email.utils import parseaddr
from datetime import datetime, timezone
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# ── Google / HubSpot config ──────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
STATE_FILE = os.getenv("SYNC_STATE_FILE", ".sync_state.json")

# Emails to ignore (own address, noreply patterns, etc.)
IGNORED_DOMAINS = {"noreply.com", "no-reply.com", "bounce.com"}
IGNORED_EMAILS: set[str] = set(
    filter(None, os.getenv("IGNORED_EMAILS", "").split(","))
)

HUBSPOT_API = "https://api.hubapi.com/crm/v3"
TAG = "Inbound Gmail"


# ── State management (tracks last processed message ID) ──────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_history_id": None, "processed_emails": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service, max_results: int = 100, page_token: str = None):
    """Return messages from INBOX excluding own-sent, promotions, spam."""
    kwargs = {
        "userId": "me",
        "labelIds": ["INBOX"],
        "q": "-from:me -category:promotions -category:updates -category:social",
        "maxResults": max_results,
    }
    if page_token:
        kwargs["pageToken"] = page_token
    result = service.users().messages().list(**kwargs).execute()
    return result.get("messages", []), result.get("nextPageToken")


def get_message_sender(service, msg_id: str) -> tuple[str, str]:
    """Return (name, email) for the From: header of a message."""
    msg = service.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From"],
    ).execute()
    for header in msg.get("payload", {}).get("headers", []):
        if header["name"].lower() == "from":
            name, email = parseaddr(header["value"])
            return name.strip(), email.strip().lower()
    return "", ""


# ── Sender parsing ────────────────────────────────────────────────────────────

def parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (firstname, lastname)."""
    parts = display_name.strip().split()
    if len(parts) == 0:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> str:
    """Heuristic: derive a company name from the email domain."""
    generic = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
               "libero.it", "virgilio.it", "tiscali.it", "alice.it"}
    if domain in generic:
        return ""
    # Strip common subdomains
    parts = domain.split(".")
    if parts[0] in ("mail", "smtp", "noreply", "no-reply", "info", "news"):
        parts = parts[1:]
    name = parts[0].replace("-", " ").replace("_", " ").title()
    return name


def should_ignore(email: str) -> bool:
    domain = email.split("@")[-1] if "@" in email else ""
    return (
        not email
        or "@" not in email
        or domain in IGNORED_DOMAINS
        or email in IGNORED_EMAILS
    )


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hubspot_search_contact(email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
    payload = {
        "filterGroups": [
            {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source"],
        "limit": 1,
    }
    r = requests.post(
        f"{HUBSPOT_API}/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hubspot_create_contact(props: dict) -> dict:
    r = requests.post(
        f"{HUBSPOT_API}/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def hubspot_update_contact(contact_id: str, props: dict) -> dict:
    r = requests.patch(
        f"{HUBSPOT_API}/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def build_contact_props(email: str, display_name: str) -> dict:
    """Build a HubSpot property dict from raw sender data."""
    firstname, lastname = parse_name(display_name)
    domain = email.split("@")[-1] if "@" in email else ""
    company = company_from_domain(domain)
    props = {
        "email": email,
        "hs_analytics_source": "OTHER_CAMPAIGNS",  # closest native option; label set via tag
        "hs_lead_status": "NEW",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


# ── Core sync logic ───────────────────────────────────────────────────────────

def sync_sender(email: str, display_name: str) -> dict:
    """
    Ensure the sender exists in HubSpot.
    Returns {"status": "Creato"|"Aggiornato"|"Ignorato", "email": ..., "contact_id": ...}
    """
    if should_ignore(email):
        return {"status": "Ignorato", "email": email, "contact_id": None}

    props = build_contact_props(email, display_name)
    existing = hubspot_search_contact(email)

    if existing is None:
        created = hubspot_create_contact(props)
        log.info("CREATO  %s  id=%s", email, created["id"])
        return {"status": "Creato", "email": email, "contact_id": created["id"]}

    contact_id = existing["id"]
    existing_props = existing.get("properties", {})
    updates = {
        k: v for k, v in props.items()
        if v and not existing_props.get(k)
    }
    if updates:
        hubspot_update_contact(contact_id, updates)
        log.info("AGGIORNATO  %s  id=%s  fields=%s", email, contact_id, list(updates))
        return {"status": "Aggiornato", "email": email, "contact_id": contact_id}

    log.info("IGNORATO  %s  id=%s  (nessuna modifica necessaria)", email, contact_id)
    return {"status": "Ignorato", "email": email, "contact_id": contact_id}


def run_sync(max_messages: int = 500) -> list[dict]:
    """Main sync loop: fetch inbox messages and sync unique senders."""
    service = get_gmail_service()
    state = load_state()

    seen_emails: set[str] = set(state.get("processed_emails", []))
    results: list[dict] = []
    page_token = None

    log.info("Avvio sync Gmail → HubSpot")
    fetched = 0

    while fetched < max_messages:
        messages, page_token = fetch_inbox_messages(service, page_token=page_token)
        if not messages:
            break

        for msg in messages:
            display_name, email = get_message_sender(service, msg["id"])
            if not email or email in seen_emails:
                continue

            seen_emails.add(email)
            result = sync_sender(email, display_name)
            results.append(result)
            time.sleep(0.1)  # gentle rate limiting

        fetched += len(messages)
        if not page_token:
            break

    # Persist processed emails (keep only last 5000 to avoid unbounded growth)
    state["processed_emails"] = list(seen_emails)[-5000:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    _print_report(results)
    return results


def _print_report(results: list[dict]) -> None:
    creati = [r for r in results if r["status"] == "Creato"]
    aggiornati = [r for r in results if r["status"] == "Aggiornato"]
    ignorati = [r for r in results if r["status"] == "Ignorato"]

    print(f"\n{'─'*60}")
    print(f"  Gmail → HubSpot Sync  ({datetime.now():%Y-%m-%d %H:%M})")
    print(f"{'─'*60}")
    print(f"  Creati:      {len(creati)}")
    print(f"  Aggiornati:  {len(aggiornati)}")
    print(f"  Ignorati:    {len(ignorati)}")
    print(f"{'─'*60}\n")

    for r in results:
        status_icon = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–"}.get(r["status"], "?")
        print(f"  {status_icon} [{r['status']:12}] {r['email']:<45}  id={r['contact_id']}")

    print()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--max", type=int, default=500, help="Max messages to scan (default 500)")
    args = parser.parse_args()

    run_sync(max_messages=args.max)
