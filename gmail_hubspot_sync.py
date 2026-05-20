#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors incoming Gmail emails and syncs sender contacts to HubSpot.

For each new email:
  - Extracts sender: email, name, company domain
  - Searches HubSpot by email (deduplication key)
  - Creates contact if not found, updates missing fields if found
  - Logs: Creato / Aggiornato / Ignorato | email | HubSpot ID
"""

import json
import logging
import os
import re
import sys
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ─── Configuration ────────────────────────────────────────────────────────────

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MAX_MESSAGES_PER_POLL = int(os.getenv("MAX_MESSAGES_PER_POLL", "50"))

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"

# Domains treated as personal (no company derived from them)
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "mac.com",
    "libero.it", "virgilio.it", "alice.it", "tiscali.it",
}

# Automated senders to skip
SKIP_PATTERNS = re.compile(
    r"^(no-?reply|noreply|do-not-reply|mailer-daemon|postmaster|bounce|"
    r"notifications?|newsletter|auto-?reply|support-noreply|unsubscribe)@",
    re.IGNORECASE,
)


# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> logging.Logger:
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler("sync.log", encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    return logging.getLogger("gmail_hubspot_sync")


log = _setup_logging()


# ─── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"processed_ids": []}


def save_state(state: dict) -> None:
    # Keep only the last 2000 processed message IDs
    ids = state.get("processed_ids", [])
    if len(ids) > 2000:
        state["processed_ids"] = ids[-2000:]
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning(f"Could not save state: {exc}")


# ─── Gmail ────────────────────────────────────────────────────────────────────

def build_gmail_service():
    creds: Optional[Credentials] = None
    token_path = Path(GMAIL_TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDENTIALS_FILE).exists():
                log.error(
                    f"Missing '{GMAIL_CREDENTIALS_FILE}'. "
                    "Download it from Google Cloud Console → APIs → Credentials."
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                GMAIL_CREDENTIALS_FILE, GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def fetch_new_messages(service, processed_ids: set) -> list[dict]:
    """Return inbox messages not yet processed, newest first."""
    try:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q="in:inbox", maxResults=MAX_MESSAGES_PER_POLL)
            .execute()
        )
    except HttpError as exc:
        log.error(f"Gmail list error: {exc}")
        return []

    refs = resp.get("messages", [])
    new_msgs: list[dict] = []

    for ref in refs:
        if ref["id"] in processed_ids:
            continue
        try:
            msg = (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=ref["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            new_msgs.append(msg)
        except HttpError as exc:
            log.warning(f"Gmail get error [{ref['id']}]: {exc}")

    return new_msgs


def parse_sender(msg: dict) -> Optional[dict]:
    """Extract and validate sender data from a Gmail message metadata dict."""
    headers = {
        h["name"]: h["value"]
        for h in msg.get("payload", {}).get("headers", [])
    }
    raw_from = headers.get("From", "").strip()
    if not raw_from:
        return None

    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    if SKIP_PATTERNS.match(email_addr):
        return None

    domain = email_addr.split("@")[1]

    # Parse first / last name from display name
    firstname = lastname = ""
    clean_name = display_name.strip().strip('"').strip("'")
    if clean_name:
        parts = clean_name.split(None, 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    # Derive company name from domain (skip personal providers)
    company = ""
    if domain not in PERSONAL_DOMAINS:
        base = domain.split(".")[0]
        company = base.capitalize()

    return {
        "message_id": msg["id"],
        "email": email_addr,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
    }


# ─── HubSpot ──────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact object or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    try:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
            headers=_hs_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("total", 0) > 0:
            return data["results"][0]
    except requests.RequestException as exc:
        log.error(f"HubSpot search error [{email}]: {exc}")
    return None


def hs_create_contact(sender: dict) -> Optional[str]:
    """Create a HubSpot contact from sender data. Returns the new contact ID."""
    props: dict = {"email": sender["email"]}
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    try:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
            headers=_hs_headers(),
            json={"properties": props},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()["id"]
    except requests.RequestException as exc:
        log.error(f"HubSpot create error [{sender['email']}]: {exc}")
    return None


def hs_update_contact(contact_id: str, updates: dict) -> bool:
    """Patch a HubSpot contact with the given property updates."""
    try:
        resp = requests.patch(
            f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
            headers=_hs_headers(),
            json={"properties": updates},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as exc:
        log.error(f"HubSpot update error [{contact_id}]: {exc}")
    return False


def hs_add_note(contact_id: str, sender: dict) -> None:
    """Create a timeline note on the contact and associate it."""
    note_body = (
        f"Email ricevuta via Gmail\n"
        f"Da: {sender['email']}\n"
        f"Oggetto: {sender['subject'] or '(nessun oggetto)'}\n"
        f"Data: {sender['date']}\n"
        f"Tag: Inbound Gmail"
    )
    # 1. Create the note object
    try:
        note_resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            headers=_hs_headers(),
            json={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                }
            },
            timeout=15,
        )
        note_resp.raise_for_status()
        note_id = note_resp.json()["id"]
    except requests.RequestException as exc:
        log.warning(f"HubSpot note create error: {exc}")
        return

    # 2. Associate the note with the contact (HUBSPOT_DEFINED type 202 = note→contact)
    try:
        assoc_resp = requests.put(
            f"{HUBSPOT_BASE}/crm/v4/objects/notes/{note_id}/associations"
            f"/contacts/{contact_id}",
            headers=_hs_headers(),
            json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            timeout=15,
        )
        assoc_resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning(f"HubSpot note association error: {exc}")


# ─── Sync Logic ───────────────────────────────────────────────────────────────

def sync_sender(sender: dict) -> tuple[str, str]:
    """
    Sync one sender to HubSpot.
    Returns (status, contact_id) where status is "Creato" | "Aggiornato" | "Ignorato".
    """
    email = sender["email"]
    existing = hs_find_contact(email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        updates: dict = {}
        if not existing_props.get("firstname") and sender["firstname"]:
            updates["firstname"] = sender["firstname"]
        if not existing_props.get("lastname") and sender["lastname"]:
            updates["lastname"] = sender["lastname"]
        if not existing_props.get("company") and sender["company"]:
            updates["company"] = sender["company"]

        if updates:
            ok = hs_update_contact(contact_id, updates)
            status = "Aggiornato" if ok else "Ignorato"
        else:
            status = "Ignorato"
    else:
        contact_id = hs_create_contact(sender)
        if not contact_id:
            return "Errore", ""
        status = "Creato"

    # Timeline note (best-effort)
    try:
        hs_add_note(contact_id, sender)
    except Exception as exc:
        log.debug(f"Note skipped: {exc}")

    return status, contact_id


# ─── Main Loop ────────────────────────────────────────────────────────────────

SEPARATOR = "─" * 62


def _print_result(status: str, email: str, contact_id: str) -> None:
    status_icons = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–", "Errore": "✗"}
    icon = status_icons.get(status, " ")
    print(f"{SEPARATOR}")
    print(f"  {icon} Stato:          {status}")
    print(f"    Email contatto: {email}")
    print(f"    ID HubSpot:     {contact_id or 'N/A'}")


def run() -> None:
    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN is not set. Add it to your .env file.")
        sys.exit(1)

    log.info("Inizializzazione Gmail...")
    gmail = build_gmail_service()

    log.info("Connessione HubSpot OK")
    state = load_state()
    processed_ids: set = set(state.get("processed_ids", []))

    log.info(f"Sync avviato — controllo ogni {POLL_INTERVAL}s")
    print(f"\n{SEPARATOR}")
    print("  Gmail → HubSpot Contact Sync  |  avviato")
    print(f"{SEPARATOR}\n")

    while True:
        log.info("Controllo nuove email...")
        messages = fetch_new_messages(gmail, processed_ids)

        if not messages:
            log.info("Nessuna nuova email.")
        else:
            log.info(f"Trovate {len(messages)} nuova/e email.")

            for msg in messages:
                msg_id = msg["id"]
                sender = parse_sender(msg)

                if sender is None:
                    log.info(f"  [{msg_id}] Saltata (mittente automatico o non valido)")
                    processed_ids.add(msg_id)
                    continue

                status, contact_id = sync_sender(sender)

                log.info(
                    f"  [{msg_id}] {status} | {sender['email']} | ID: {contact_id}"
                )
                _print_result(status, sender["email"], contact_id)

                processed_ids.add(msg_id)
                state["processed_ids"] = list(processed_ids)
                save_state(state)

                time.sleep(0.3)  # gentle rate-limit buffer

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
