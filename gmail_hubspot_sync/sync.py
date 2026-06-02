#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox for new emails and automatically syncs sender contacts to HubSpot.
Avoids duplicates, updates existing records, and logs every action.
"""

import os
import re
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# ── Config ─────────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.labels"]
GMAIL_CREDENTIALS_FILE = Path(os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"))
GMAIL_TOKEN_FILE = Path(os.getenv("GMAIL_TOKEN_FILE", "token.json"))
HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
STATE_FILE = Path(os.getenv("STATE_FILE", ".sync_state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default

# Domains to skip (internal / system senders)
SKIP_DOMAINS = {
    "googlemail.com", "google.com", "mailer-daemon.googlemail.com",
}
# Explicit addresses to skip (self)
SKIP_ADDRESSES: set[str] = set(
    filter(None, os.getenv("SKIP_ADDRESSES", "").lower().split(","))
)

HUBSPOT_BASE = "https://api.hubapi.com"
CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Data model ──────────────────────────────────────────────────────────────
@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = CONTACT_SOURCE
    tag: str = CONTACT_TAG
    thread_id: str = ""
    subject: str = ""


# ── Gmail helpers ────────────────────────────────────────────────────────────
def gmail_service():
    creds = None
    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_thread_ids": [], "last_run": None}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


_FWD_PATTERN = re.compile(
    r'Da[:\s]+"?([^"<\n]+?)"?\s*<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
    re.IGNORECASE,
)
_NAME_EMAIL_PATTERN = re.compile(
    r'"?([^"<@\n]{2,50})"?\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>'
)


def parse_sender(from_header: str) -> tuple[str, str, str]:
    """Returns (email, firstname, lastname)."""
    m = _NAME_EMAIL_PATTERN.match(from_header.strip())
    if m:
        raw_name = m.group(1).strip()
        email = m.group(2).strip().lower()
    else:
        email = from_header.strip().lower()
        raw_name = ""
    parts = raw_name.split(None, 1)
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""
    return email, firstname, lastname


def domain_to_company(email: str) -> str:
    """Derive a readable company name from the email domain."""
    domain = email.split("@")[-1]
    # strip common TLDs and format
    base = re.sub(r'\.(com|org|it|eu|net|gov|edu|io|ch|de|fr|uk|co\.uk)$', '', domain)
    return base.replace("-", " ").replace(".", " ").title()


def extract_contacts_from_thread(thread: dict) -> list[Contact]:
    contacts: list[Contact] = []
    for msg in thread.get("messages", []):
        sender = msg.get("sender", "")
        subject = msg.get("subject", "")
        snippet = msg.get("snippet", "")
        thread_id = msg.get("id", "")

        if not sender:
            continue

        email, firstname, lastname = parse_sender(sender)
        if _should_skip(email):
            # Try to extract forwarded-message sender from snippet
            fwd = _extract_forwarded(snippet, thread_id, subject)
            if fwd:
                contacts.extend(fwd)
            continue

        company = domain_to_company(email)
        contacts.append(Contact(
            email=email, firstname=firstname, lastname=lastname,
            company=company, thread_id=thread_id, subject=subject,
        ))

        # Also check snippet for embedded forwarded senders
        fwd = _extract_forwarded(snippet, thread_id, subject)
        if fwd:
            contacts.extend(fwd)

    return contacts


def _should_skip(email: str) -> bool:
    domain = email.split("@")[-1].lower()
    if domain in SKIP_DOMAINS:
        return True
    if email.lower() in SKIP_ADDRESSES:
        return True
    if email.startswith("mailer-daemon"):
        return True
    if email.startswith("noreply") or email.startswith("no-reply"):
        return True
    return False


def _extract_forwarded(snippet: str, thread_id: str, subject: str) -> list[Contact]:
    contacts = []
    for m in _FWD_PATTERN.finditer(snippet):
        name = m.group(1).strip().strip('"')
        email = m.group(2).strip().lower()
        if _should_skip(email):
            continue
        parts = name.split(None, 1)
        firstname = parts[0] if parts else ""
        lastname = parts[1] if len(parts) > 1 else ""
        company = domain_to_company(email)
        contacts.append(Contact(
            email=email, firstname=firstname, lastname=lastname,
            company=company, thread_id=thread_id, subject=subject,
        ))
    return contacts


def fetch_new_threads(svc, processed_ids: list[str], query: str = "in:inbox -from:me") -> list[dict]:
    result = svc.users().threads().list(userId="me", q=query, maxResults=50).execute()
    threads = result.get("threads", [])
    new_threads = []
    for t in threads:
        if t["id"] not in processed_ids:
            full = svc.users().threads().get(
                userId="me", id=t["id"],
                format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            messages = []
            for msg in full.get("messages", []):
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                messages.append({
                    "id": msg["id"],
                    "labelIds": msg.get("labelIds", []),
                    "sender": headers.get("From", ""),
                    "subject": headers.get("Subject", ""),
                    "snippet": msg.get("snippet", ""),
                })
            new_threads.append({"id": t["id"], "messages": messages})
    return new_threads


# ── HubSpot helpers ──────────────────────────────────────────────────────────
def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type": "application/json",
    }


def search_contact(email: str) -> Optional[dict]:
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source", "hs_tag_ids"],
        "limit": 1,
    }
    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
                      headers=_hs_headers(), json=body)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def create_contact(c: Contact) -> dict:
    props = {"email": c.email}
    if c.firstname:
        props["firstname"] = c.firstname
    if c.lastname:
        props["lastname"] = c.lastname
    if c.company:
        props["company"] = c.company
    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
                      headers=_hs_headers(), json={"properties": props})
    r.raise_for_status()
    return r.json()


def update_contact(contact_id: str, c: Contact, existing: dict) -> dict:
    existing_props = existing.get("properties", {})
    props = {}
    # Only fill missing fields
    if c.firstname and not existing_props.get("firstname"):
        props["firstname"] = c.firstname
    if c.lastname and not existing_props.get("lastname"):
        props["lastname"] = c.lastname
    if c.company and not existing_props.get("company"):
        props["company"] = c.company
    if not props:
        return existing  # nothing to update
    r = requests.patch(f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
                       headers=_hs_headers(), json={"properties": props})
    r.raise_for_status()
    return r.json()


def add_note(contact_id: str, c: Contact):
    """Create a note activity on the contact timeline."""
    body = {
        "properties": {
            "hs_note_body": f"Email ricevuta da Gmail\nOggetto: {c.subject}\nFonte: {CONTACT_SOURCE}",
            "hs_timestamp": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        }],
    }
    r = requests.post(f"{HUBSPOT_BASE}/crm/v3/objects/notes",
                      headers=_hs_headers(), json=body)
    if not r.ok:
        log.warning("Note creation failed for contact %s: %s", contact_id, r.text)


# ── Sync logic ───────────────────────────────────────────────────────────────
@dataclass
class SyncResult:
    status: str  # "Creato" | "Aggiornato" | "Ignorato"
    email: str
    contact_id: str = ""
    reason: str = ""


def sync_contact(c: Contact, add_notes: bool = True) -> SyncResult:
    try:
        existing = search_contact(c.email)
        if existing:
            contact_id = existing["id"]
            updated = update_contact(contact_id, c, existing)
            changed = updated != existing
            if add_notes:
                add_note(contact_id, c)
            return SyncResult("Aggiornato", c.email, contact_id)
        else:
            created = create_contact(c)
            contact_id = created["id"]
            if add_notes:
                add_note(contact_id, c)
            return SyncResult("Creato", c.email, contact_id)
    except requests.HTTPError as e:
        log.error("HubSpot error for %s: %s", c.email, e.response.text)
        return SyncResult("Ignorato", c.email, reason=str(e))


def run_sync(svc, state: dict, add_notes: bool = True) -> list[SyncResult]:
    processed_ids = state.get("processed_thread_ids", [])
    threads = fetch_new_threads(svc, processed_ids)
    log.info("Found %d new threads to process", len(threads))

    # Collect all contacts, deduplicate by email
    seen: dict[str, Contact] = {}
    for thread in threads:
        for c in extract_contacts_from_thread(thread):
            if c.email not in seen:
                seen[c.email] = c

    results: list[SyncResult] = []
    for c in seen.values():
        result = sync_contact(c, add_notes=add_notes)
        results.append(result)
        log.info("[%s] %s (ID: %s)", result.status, result.email, result.contact_id)

    # Update state
    new_ids = [t["id"] for t in threads]
    state["processed_thread_ids"] = list(set(processed_ids + new_ids))[-500:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    return results


def print_report(results: list[SyncResult]):
    print("\n" + "=" * 60)
    print(f"{'STATO':<12} {'EMAIL':<40} {'ID HUBSPOT'}")
    print("-" * 60)
    counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}
    for r in results:
        print(f"{r.status:<12} {r.email:<40} {r.contact_id or r.reason}")
        counts[r.status] = counts.get(r.status, 0) + 1
    print("=" * 60)
    print(f"Totale: {len(results)} | Creati: {counts['Creato']} | "
          f"Aggiornati: {counts['Aggiornato']} | Ignorati: {counts['Ignorato']}")


# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Run once and exit (no loop)")
    parser.add_argument("--no-notes", action="store_true", help="Skip creating HubSpot notes")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL,
                        help="Polling interval in seconds (default: 300)")
    args = parser.parse_args()

    if not HUBSPOT_API_KEY:
        raise SystemExit("ERROR: HUBSPOT_API_KEY environment variable not set.")

    svc = gmail_service()
    state = load_state()

    while True:
        log.info("Starting sync run...")
        results = run_sync(svc, state, add_notes=not args.no_notes)
        print_report(results)

        if args.once:
            break
        log.info("Next run in %d seconds. Ctrl+C to stop.", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
