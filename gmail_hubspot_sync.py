#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.
For each new email: extracts sender info, creates or updates the HubSpot
contact (deduplicating by email), and logs a timeline activity note.
"""

import os
import json
import time
import logging
import email.utils
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
HUBSPOT_TOKEN         = os.getenv("HUBSPOT_API_TOKEN", "")
GMAIL_CREDS_FILE      = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE      = os.getenv("GMAIL_TOKEN_FILE", "gmail_token.json")
POLL_INTERVAL         = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE            = os.getenv("STATE_FILE", "processed_emails.json")
INITIAL_LOOKBACK_DAYS = int(os.getenv("INITIAL_LOOKBACK_DAYS", "7"))
MAX_STATE_SIZE        = 50_000

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"

# Common personal/free email providers — skip company extraction for these
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "ymail.com",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it", "live.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com", "proton.me",
    "mail.com", "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "tin.it", "email.it", "msn.com",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── State persistence ──────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed": []}


def save_state(state: dict):
    ids = state.get("processed", [])
    if len(ids) > MAX_STATE_SIZE:
        ids = ids[-MAX_STATE_SIZE:]
    state["processed"] = ids
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Gmail ──────────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(GMAIL_TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(GMAIL_CREDS_FILE).exists():
                raise FileNotFoundError(
                    f"File credenziali Gmail non trovato: {GMAIL_CREDS_FILE}\n"
                    "Scaricalo da Google Cloud Console → API e servizi → Credenziali."
                )
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(GMAIL_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def fetch_new_emails(service, processed_ids: set, since_date: str) -> list:
    """Return unread inbox emails received after since_date that haven't been processed."""
    query = f"in:inbox is:unread after:{since_date}"
    unprocessed_ids = []
    page_token = None

    while True:
        kwargs = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            kwargs["pageToken"] = page_token
        try:
            result = service.users().messages().list(**kwargs).execute()
        except HttpError as e:
            log.error(f"Errore Gmail API (list): {e}")
            break

        for msg in result.get("messages", []):
            if msg["id"] not in processed_ids:
                unprocessed_ids.append(msg["id"])

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    detailed = []
    for msg_id in unprocessed_ids:
        try:
            detail = service.users().messages().get(
                userId="me", id=msg_id, format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            ).execute()
            headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
            detailed.append({
                "id": msg_id,
                "from": headers.get("From", ""),
                "subject": headers.get("Subject", "(nessun oggetto)"),
                "date": headers.get("Date", ""),
            })
        except HttpError as e:
            log.warning(f"Impossibile recuperare email {msg_id}: {e}")

    return detailed


# ── Sender parsing ─────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> Optional[dict]:
    """Parse a From: header into structured contact fields."""
    name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()
    if not addr or "@" not in addr:
        return None

    domain = addr.split("@")[1]
    parts = name.strip().split(" ", 1) if name.strip() else []
    firstname = parts[0] if parts else ""
    lastname = parts[1] if len(parts) > 1 else ""

    company = ""
    if domain not in PERSONAL_DOMAINS:
        domain_parts = domain.split(".")
        if len(domain_parts) >= 2:
            company = domain_parts[-2].capitalize()

    return {
        "email": addr,
        "name": name.strip(),
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
    }


# ── HubSpot ────────────────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_search_contact(email_addr: str) -> Optional[dict]:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{"propertyName": "email", "operator": "EQ", "value": email_addr}]
        }],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = requests.post(url, json=payload, headers=_hs_headers(), timeout=10)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> dict:
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    props = {"email": sender["email"]}
    for field in ("firstname", "lastname", "company"):
        if sender.get(field):
            props[field] = sender[field]
    r = requests.post(url, json={"properties": props}, headers=_hs_headers(), timeout=10)
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, sender: dict, existing_props: dict) -> bool:
    """Fill in blank fields only. Returns True if an update was sent."""
    props = {}
    for field in ("firstname", "lastname", "company"):
        if sender.get(field) and not existing_props.get(field):
            props[field] = sender[field]
    if not props:
        return False
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    r = requests.patch(url, json={"properties": props}, headers=_hs_headers(), timeout=10)
    r.raise_for_status()
    return True


def hs_add_email_note(contact_id: str, sender: dict, subject: str, date: str):
    """Create a timeline note on the contact recording the received email."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    body = (
        "Email ricevuta via Gmail\n"
        f"Da: {sender.get('name', '')} <{sender.get('email', '')}>\n"
        f"Oggetto: {subject}\n"
        f"Data: {date}\n"
        "Tag: Inbound Gmail\n"
        "Fonte contatto: Gmail"
    )
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(int(time.time() * 1000)),
        },
        "associations": [{
            "to": {"id": contact_id},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": 202,
            }],
        }],
    }
    r = requests.post(url, json=payload, headers=_hs_headers(), timeout=10)
    if not r.ok:
        log.warning(
            f"Nota non creata per contatto {contact_id}: "
            f"{r.status_code} {r.text[:200]}"
        )


# ── Core logic ─────────────────────────────────────────────────────────────────

def process_email(email_data: dict) -> dict:
    """Process a single email: create/update HubSpot contact and log activity."""
    sender = parse_sender(email_data["from"])
    if not sender:
        return {
            "status": "Ignorato",
            "email": email_data.get("from", "?"),
            "hubspot_id": None,
            "reason": "Indirizzo email non valido",
        }

    try:
        existing = hs_search_contact(sender["email"])
        if existing:
            contact_id = existing["id"]
            hs_update_contact(contact_id, sender, existing.get("properties", {}))
            hs_add_email_note(contact_id, sender, email_data["subject"], email_data["date"])
            return {"status": "Aggiornato", "email": sender["email"], "hubspot_id": contact_id}
        else:
            created = hs_create_contact(sender)
            contact_id = created["id"]
            hs_add_email_note(contact_id, sender, email_data["subject"], email_data["date"])
            return {"status": "Creato", "email": sender["email"], "hubspot_id": contact_id}

    except requests.HTTPError as e:
        body = e.response.text[:200] if e.response else ""
        log.error(f"HubSpot API error per {sender['email']}: {e.response.status_code} {body}")
        return {
            "status": "Errore",
            "email": sender["email"],
            "hubspot_id": None,
            "reason": str(e),
        }


def log_result(result: dict):
    icons = {"Creato": "[+]", "Aggiornato": "[~]", "Ignorato": "[-]", "Errore": "[!]"}
    icon = icons.get(result["status"], "[ ]")
    hs_id = result.get("hubspot_id") or "N/A"
    log.info(f"{icon} {result['status']:<10} | {result['email']:<42} | HubSpot ID: {hs_id}")


# ── Entry point ────────────────────────────────────────────────────────────────

def run():
    if not HUBSPOT_TOKEN:
        raise SystemExit(
            "ERRORE: HUBSPOT_API_TOKEN non impostato.\n"
            "Copia .env.example in .env e inserisci il tuo token HubSpot."
        )

    log.info("=" * 60)
    log.info("  Gmail → HubSpot Contact Sync  (avviato)")
    log.info("=" * 60)

    service = get_gmail_service()
    state = load_state()
    processed_ids = set(state.get("processed", []))
    log.info(f"Cache caricata: {len(processed_ids)} email già processate")

    lookback = datetime.now(timezone.utc) - timedelta(days=INITIAL_LOOKBACK_DAYS)
    since_date = lookback.strftime("%Y/%m/%d")
    log.info(f"Finestra iniziale: email dal {since_date} | Polling ogni {POLL_INTERVAL}s")

    while True:
        try:
            log.info("─" * 40)
            log.info("Controllo nuove email...")
            new_emails = fetch_new_emails(service, processed_ids, since_date)

            if not new_emails:
                log.info("Nessuna nuova email.")
            else:
                log.info(f"{len(new_emails)} nuova/e email trovata/e.")
                for email_data in new_emails:
                    result = process_email(email_data)
                    log_result(result)
                    processed_ids.add(email_data["id"])

                state["processed"] = list(processed_ids)
                save_state(state)

        except HttpError as e:
            log.error(f"Errore Gmail API: {e}")
        except Exception as e:
            log.error(f"Errore imprevisto: {e}", exc_info=True)

        log.info(f"Prossimo controllo tra {POLL_INTERVAL}s...")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    run()
