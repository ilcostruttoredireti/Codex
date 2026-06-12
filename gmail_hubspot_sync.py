#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la inbox Gmail e sincronizza i mittenti come contatti HubSpot.
Evita duplicati usando l'email come chiave unica.

Usage:
    python gmail_hubspot_sync.py            # run once
    python gmail_hubspot_sync.py --loop     # continuous polling
"""

import os
import re
import json
import time
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from email.utils import parseaddr

import requests
from dotenv import load_dotenv

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
LOG_FILE = Path(os.getenv("LOG_FILE", "sync.log"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))  # 5 min default

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Personal/generic domains to skip for company extraction
GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "icloud.com", "me.com", "live.com", "live.it",
}

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ─── Gmail Auth ───────────────────────────────────────────────────────────────

def get_gmail_service():
    """Build authenticated Gmail API service."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        raise SystemExit(
            "Missing Google libraries. Run: pip install google-api-python-client "
            "google-auth-httplib2 google-auth-oauthlib"
        )

    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        Path(GMAIL_TOKEN_FILE).write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)

# ─── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"processed_message_ids": [], "last_run": None}

def save_state(state: dict):
    # Keep only last 2000 IDs to avoid unbounded growth
    state["processed_message_ids"] = state["processed_message_ids"][-2000:]
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))

# ─── Email Parsing ────────────────────────────────────────────────────────────

# Matches: Da "Nome Cognome" email@domain.com   (Italian forwarding)
_FWD_IT = re.compile(
    r'Da\s+"([^"]{1,80})"\s+([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
    re.IGNORECASE,
)
# Matches: From: "Name" <email>  or  From: Name <email>
_FWD_EN = re.compile(
    r'From:\s+"?([^"<\n]{1,80})"?\s+<([\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>',
    re.IGNORECASE,
)
# Bare email fallback
_EMAIL_RE = re.compile(r'[\w._%+\-]+@[\w.\-]+\.[a-zA-Z]{2,}')


def _split_name(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (full_name.strip(), "")


def _company_from_domain(domain: str) -> str:
    if not domain or domain in GENERIC_DOMAINS:
        return ""
    parts = domain.rstrip(".").split(".")
    raw = parts[-2] if len(parts) >= 2 else parts[0]
    return raw.replace("-", " ").replace("_", " ").title()


def extract_contacts(message: dict) -> list[dict]:
    """
    Return list of contact dicts from a Gmail message.
    Handles direct sender + Italian forwarded format.
    Deduplicates by email within the same message.
    """
    contacts: list[dict] = []
    seen: set[str] = set()

    def add(email: str, name: str = ""):
        email = email.strip().lower()
        if not email or email in seen:
            return
        seen.add(email)
        domain = email.split("@")[-1] if "@" in email else ""
        fname, lname = _split_name(name) if name else ("", "")
        contacts.append({
            "email": email,
            "firstname": fname,
            "lastname": lname,
            "company": _company_from_domain(domain),
            "domain": domain,
        })

    headers = {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    # 1. Direct From: header
    raw_from = headers.get("from", "")
    name, addr = parseaddr(raw_from)
    if addr:
        add(addr, name)

    # 2. Forwarded senders in snippet / body
    text = message.get("snippet", "")
    for pat in (_FWD_IT, _FWD_EN):
        for m in pat.finditer(text):
            add(m.group(2), m.group(1))

    return contacts

# ─── HubSpot API ──────────────────────────────────────────────────────────────

HS_BASE = "https://api.hubapi.com/crm/v3/objects/contacts"


def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


def hs_find_contact(email: str) -> Optional[dict]:
    resp = requests.post(
        f"{HS_BASE}/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(contact: dict) -> dict:
    props: dict[str, str] = {"email": contact["email"], "hs_lead_source": "Gmail"}
    for field in ("firstname", "lastname", "company"):
        if contact.get(field):
            props[field] = contact[field]
    resp = requests.post(HS_BASE, headers=_hs_headers(), json={"properties": props}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, contact: dict, existing_props: dict) -> bool:
    """Patch only fields that are missing in HubSpot. Returns True if anything changed."""
    updates: dict[str, str] = {}
    for field in ("firstname", "lastname", "company"):
        if contact.get(field) and not existing_props.get(field):
            updates[field] = contact[field]
    if not updates:
        return False
    resp = requests.patch(
        f"{HS_BASE}/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=15,
    )
    resp.raise_for_status()
    return True


def hs_add_note(contact_id: str, email_subject: str, email_date: str):
    """Create a Note activity on the contact (optional timeline entry)."""
    url = "https://api.hubapi.com/crm/v3/objects/notes"
    note_body = (
        f"📧 Email ricevuta tramite Gmail\n"
        f"Oggetto: {email_subject}\n"
        f"Data: {email_date}\n"
        f"Fonte: Inbound Gmail"
    )
    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": datetime.now(timezone.utc).isoformat(),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    resp = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    resp.raise_for_status()

# ─── Sync Logic ───────────────────────────────────────────────────────────────

def sync_contact(contact: dict, email_subject: str = "", email_date: str = "") -> dict:
    existing = hs_find_contact(contact["email"])
    if existing:
        contact_id = existing["id"]
        changed = hs_update_contact(contact_id, contact, existing.get("properties", {}))
        status = "Aggiornato" if changed else "Ignorato"
    else:
        created = hs_create_contact(contact)
        contact_id = created["id"]
        status = "Creato"
        if email_subject:
            try:
                hs_add_note(contact_id, email_subject, email_date)
            except Exception as e:
                log.warning(f"Note creation failed for {contact['email']}: {e}")

    return {"status": status, "email": contact["email"], "hubspot_id": contact_id}


def process_messages(service, message_ids: list[str], state: dict) -> list[dict]:
    results = []
    session_seen: set[str] = set()

    for msg_id in message_ids:
        if msg_id in state["processed_message_ids"]:
            continue

        try:
            msg = service.users().messages().get(userId="me", id=msg_id, format="full").execute()
        except Exception as e:
            log.error(f"Could not fetch message {msg_id}: {e}")
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("subject", "")
        date = headers.get("date", "")

        contacts = extract_contacts(msg)
        for contact in contacts:
            email_addr = contact["email"]
            if email_addr in session_seen:
                continue
            session_seen.add(email_addr)
            try:
                result = sync_contact(contact, subject, date)
                results.append(result)
                log.info(
                    f"[{result['status']:10s}] {result['email']:<45} → ID {result['hubspot_id']}"
                )
            except Exception as e:
                log.error(f"HubSpot error for {email_addr}: {e}")
                results.append({"status": "Errore", "email": email_addr, "hubspot_id": None})

        state["processed_message_ids"].append(msg_id)

    return results

# ─── Main ─────────────────────────────────────────────────────────────────────

def run_once(service, state: dict, query: str = "in:inbox") -> list[dict]:
    if state.get("last_run"):
        query += " newer_than:1d"
    log.info(f"Gmail query: {query!r}")
    response = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
    msgs = response.get("messages", [])
    log.info(f"Trovati {len(msgs)} messaggi")
    results = process_messages(service, [m["id"] for m in msgs], state)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    return results


def print_report(results: list[dict]):
    if not results:
        print("\nNessun nuovo contatto elaborato.")
        return
    print(f"\n{'─'*70}")
    print(f"{'Stato':<12} {'Email':<45} {'HubSpot ID'}")
    print(f"{'─'*70}")
    for r in results:
        print(f"{r['status']:<12} {r['email']:<45} {r['hubspot_id'] or 'N/A'}")
    print(f"{'─'*70}")
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    summary = "  ".join(f"{s}: {n}" for s, n in counts.items())
    print(f"Totale: {len(results)} contatti  |  {summary}\n")


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--loop", action="store_true", help="Polling continuo")
    parser.add_argument("--query", default="in:inbox", help="Gmail search query")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL)
    args = parser.parse_args()

    if not HUBSPOT_API_KEY:
        raise SystemExit("HUBSPOT_API_KEY non configurata. Imposta la variabile in .env")

    service = get_gmail_service()

    if args.loop:
        log.info(f"Avvio polling ogni {args.interval}s")
        while True:
            state = load_state()
            try:
                results = run_once(service, state, args.query)
                print_report(results)
            except Exception as e:
                log.error(f"Errore ciclo sync: {e}", exc_info=True)
            save_state(state)
            time.sleep(args.interval)
    else:
        state = load_state()
        results = run_once(service, state, args.query)
        print_report(results)
        save_state(state)


if __name__ == "__main__":
    main()
