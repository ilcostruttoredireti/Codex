#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox for new emails and upserts sender contacts into HubSpot.

Usage:
    python gmail_hubspot_sync.py            # run once
    python gmail_hubspot_sync.py --daemon   # poll continuously

Required env vars:
    HUBSPOT_ACCESS_TOKEN  – HubSpot Private App token
    GMAIL_CREDENTIALS_FILE – path to Google OAuth credentials JSON (default: credentials.json)

Optional env vars:
    GMAIL_TOKEN_FILE    – path to cached OAuth token (default: token.json)
    POLL_INTERVAL       – seconds between polls in daemon mode (default: 60)
    STATE_FILE          – path to processed-IDs state file (default: processed_threads.json)
    GMAIL_QUERY         – Gmail search query (default: in:inbox -from:me)
    LOG_FILE            – log file path (default: sync.log)
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ──────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_QUERY = os.environ.get("GMAIL_QUERY", "in:inbox -from:me")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "processed_threads.json"))
HUBSPOT_TOKEN = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
LOG_FILE = os.environ.get("LOG_FILE", "sync.log")
HUBSPOT_BASE = "https://api.hubapi.com"

# Domains and local-part prefixes from automated / notification senders to skip
_IGNORED_DOMAINS = frozenset({
    "facebookmail.com", "twitter.com", "linkedin.com", "youtube.com",
    "instagram.com", "amazonses.com", "sendgrid.net", "mailchimp.com",
    "bounce.com", "mandrillapp.com", "sparkpostmail.com",
})
_IGNORED_PREFIXES = frozenset({
    "noreply", "no-reply", "mailer-daemon", "postmaster",
    "bounce", "notification", "notifications", "donotreply",
    "do-not-reply", "auto-reply", "autoreply", "support+",
})


# ── Logging ────────────────────────────────────────────────────────────────────
def _setup_logging():
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers = [logging.StreamHandler()]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)


log = logging.getLogger(__name__)


# ── Gmail ──────────────────────────────────────────────────────────────────────
def _get_gmail_service():
    creds = None
    token_path = Path(GMAIL_TOKEN)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS).exists():
                log.error(
                    "File credenziali Gmail non trovato: %s\n"
                    "Scarica il file da Google Cloud Console → API & Services → Credentials "
                    "e salvalo come credentials.json nella directory di lavoro.",
                    GMAIL_CREDENTIALS,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDENTIALS, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def _fetch_messages(service, max_results=100):
    """Return list of {id, threadId} dicts from Gmail matching GMAIL_QUERY."""
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", q=GMAIL_QUERY, maxResults=max_results)
            .execute()
        )
        return result.get("messages", [])
    except HttpError as exc:
        log.error("Errore Gmail API: %s", exc)
        return []


def _get_sender_info(service, msg_id):
    """Return (name, email, subject, date_str) for a message."""
    try:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
    except HttpError as exc:
        log.warning("Impossibile recuperare messaggio %s: %s", msg_id, exc)
        return None, None, "", ""

    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    raw_from = headers.get("From", "")
    name, email_addr = parseaddr(raw_from)
    return (
        name.strip(),
        email_addr.strip().lower(),
        headers.get("Subject", "(nessun oggetto)"),
        headers.get("Date", ""),
    )


# ── Filtering ──────────────────────────────────────────────────────────────────
def _should_ignore(email_addr: str) -> bool:
    """Return True for automated, notification, or invalid senders."""
    if not email_addr or "@" not in email_addr:
        return True
    local, domain = email_addr.split("@", 1)
    if domain in _IGNORED_DOMAINS:
        return True
    local_lower = local.lower()
    return any(local_lower.startswith(p) for p in _IGNORED_PREFIXES)


# ── Name / company helpers ─────────────────────────────────────────────────────
def _split_name(display_name: str):
    """Return (firstname, lastname) from a display name string."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    # Strip common subdomains (mail., smtp., etc.)
    parts = domain.split(".")
    # Take the second-to-last part as the brand name (e.g. "acme" from "acme.com")
    brand = parts[-2] if len(parts) >= 2 else parts[0]
    return re.sub(r"[-_]", " ", brand).title()


# ── HubSpot API ────────────────────────────────────────────────────────────────
def _hs_headers():
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _hs_search_contact(email_addr: str):
    """Return existing contact dict or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email_addr}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def _hs_create_contact(email_addr, firstname, lastname, company):
    props = {
        "email": email_addr,
        "hs_lead_source": "OTHER",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _hs_update_contact(contact_id: int, current_props: dict, firstname, lastname, company):
    """Patch only fields that are currently empty/missing. Returns True if anything was updated."""
    updates = {}
    if firstname and not current_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not current_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not current_props.get("company"):
        updates["company"] = company
    # Always set lead_source if not already from Gmail context
    if not current_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "OTHER"

    if not updates:
        return False

    r = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=15,
    )
    r.raise_for_status()
    return True


def _hs_add_note(contact_id: int, email_addr: str, subject: str, received_at: str):
    """Attach a timeline Note to the contact."""
    timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    body = (
        f"📧 Email ricevuta\n"
        f"Da: {email_addr}\n"
        f"Oggetto: {subject}\n"
        f"Data: {received_at}\n"
        f"Fonte: Gmail\n"
        f"Tag: Inbound Gmail"
    )
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": timestamp_ms,
        },
        "associations": [
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
    }
    r = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json=payload,
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── State management ───────────────────────────────────────────────────────────
def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"processed_message_ids": []}


def _save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ── Core processing ────────────────────────────────────────────────────────────
def _process_message(service, msg_id: str, state: dict):
    if msg_id in state["processed_message_ids"]:
        return  # already handled

    name, email_addr, subject, received_at = _get_sender_info(service, msg_id)

    if not email_addr:
        state["processed_message_ids"].append(msg_id)
        return

    if _should_ignore(email_addr):
        log.info("%-12s | %s", "IGNORATO", email_addr)
        state["processed_message_ids"].append(msg_id)
        return

    domain = email_addr.split("@")[1]
    firstname, lastname = _split_name(name)
    company = _company_from_domain(domain)

    status = ""
    contact_id = None

    try:
        existing = _hs_search_contact(email_addr)

        if existing:
            contact_id = int(existing["id"])
            current_props = existing.get("properties", {})
            updated = _hs_update_contact(contact_id, current_props, firstname, lastname, company)
            status = "AGGIORNATO" if updated else "INVARIATO"
        else:
            created = _hs_create_contact(email_addr, firstname, lastname, company)
            contact_id = int(created["id"])
            status = "CREATO"

        # Timeline note
        try:
            _hs_add_note(contact_id, email_addr, subject, received_at)
        except requests.HTTPError as exc:
            log.warning("Nota non aggiunta per %s: %s", email_addr, exc)

        log.info("%-12s | %-45s | HubSpot ID: %s", status, email_addr, contact_id)

    except requests.HTTPError as exc:
        log.error("Errore HubSpot per %s: %s", email_addr, exc.response.text if exc.response else exc)

    state["processed_message_ids"].append(msg_id)


def _run_once(service):
    state = _load_state()
    messages = _fetch_messages(service)

    if not messages:
        log.info("Nessun messaggio trovato con query: %r", GMAIL_QUERY)
        _save_state(state)
        return

    log.info("Trovati %d messaggi — inizio sincronizzazione…", len(messages))
    for msg in messages:
        _process_message(service, msg["id"], state)

    _save_state(state)
    log.info("Sincronizzazione completata.")


def _run_daemon(service):
    log.info("Modalità daemon avviata — polling ogni %ds (query: %r)", POLL_INTERVAL, GMAIL_QUERY)
    while True:
        try:
            _run_once(service)
        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL)


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    _setup_logging()

    if not HUBSPOT_TOKEN:
        log.error(
            "HUBSPOT_ACCESS_TOKEN non impostato.\n"
            "Crea un Private App su HubSpot → Settings → Integrations → Private Apps\n"
            "e assegna gli scope: crm.objects.contacts.read, crm.objects.contacts.write, crm.objects.notes.write"
        )
        sys.exit(1)

    service = _get_gmail_service()

    if "--daemon" in sys.argv:
        _run_daemon(service)
    else:
        _run_once(service)


if __name__ == "__main__":
    main()
