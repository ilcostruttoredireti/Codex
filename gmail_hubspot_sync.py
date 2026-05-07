#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors the Gmail inbox and syncs senders to HubSpot contacts.

Output per email:
  Stato        : Creato / Aggiornato / Ignorato
  Email        : indirizzo mittente
  ID contatto  : ID HubSpot del contatto (se creato/aggiornato)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
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

# ── Configuration (from environment / .env) ───────────────────────────────────
HUBSPOT_TOKEN    = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
CREDENTIALS_FILE = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE       = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE       = os.environ.get("STATE_FILE", ".sync_state.json")
POLL_INTERVAL    = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
MY_EMAIL         = os.environ.get("MY_EMAIL", "").lower().strip()
SKIP_DOMAINS     = {
    d.strip() for d in os.environ.get("SKIP_DOMAINS", "").split(",") if d.strip()
}
DRY_RUN          = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
RUN_ONCE         = "--once" in sys.argv   # single scan then exit (useful for testing)

GMAIL_SCOPES     = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE     = "https://api.hubapi.com"

# Domains that belong to generic mail providers (no company name inferred)
GENERIC_DOMAINS  = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com",
    "icloud.com", "me.com", "libero.it", "virgilio.it", "tiscali.it",
}

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# State persistence
# ══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_message_ids": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# Gmail
# ══════════════════════════════════════════════════════════════════════════════

def get_gmail_service():
    """Authenticate with OAuth2 and return a Gmail API client."""
    creds: Optional[Credentials] = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                log.error(
                    "File credenziali Gmail non trovato: %s\n"
                    "Scaricalo da Google Cloud Console → API e servizi → Credenziali.",
                    CREDENTIALS_FILE,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_new_messages(service, processed_ids: set) -> list[dict]:
    """Return unprocessed inbox messages (up to 50, newest-first)."""
    try:
        resp = service.users().messages().list(
            userId="me",
            labelIds=["INBOX"],
            maxResults=50,
        ).execute()
        return [m for m in resp.get("messages", []) if m["id"] not in processed_ids]
    except HttpError as e:
        log.error("Gmail list error: %s", e)
        return []


def get_message_details(service, msg_id: str) -> Optional[dict]:
    """Fetch key headers for a single message."""
    try:
        msg = service.users().messages().get(
            userId="me",
            id=msg_id,
            format="metadata",
            metadataHeaders=["From", "Subject"],
        ).execute()
        hdrs = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        return {
            "id": msg_id,
            "from": hdrs.get("From", ""),
            "subject": hdrs.get("Subject", "(nessun oggetto)"),
            "internal_date_ms": int(msg.get("internalDate", "0")),
        }
    except HttpError as e:
        log.error("Gmail get error for %s: %s", msg_id, e)
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Parsing helpers
# ══════════════════════════════════════════════════════════════════════════════

def parse_sender(from_header: str) -> tuple[str, str, str]:
    """
    Parse 'Display Name <email@domain>' → (email, firstname, lastname).
    Falls back to splitting the local-part when no display name is present.
    """
    display_name, email = parseaddr(from_header)
    email        = email.lower().strip()
    display_name = display_name.strip()

    if display_name:
        parts     = display_name.split(None, 1)
        firstname = parts[0].strip()
        lastname  = parts[1].strip() if len(parts) > 1 else ""
    else:
        local     = email.split("@")[0] if "@" in email else email
        firstname = (
            local.replace(".", " ")
                 .replace("_", " ")
                 .replace("-", " ")
                 .strip()
                 .title()
        )
        lastname  = ""

    return email, firstname, lastname


def domain_to_company(email: str) -> str:
    """Derive a human-readable company name from the email domain."""
    if "@" not in email:
        return ""
    domain = email.split("@")[1]
    # Strip common mail subdomains
    for prefix in (
        "mail.", "email.", "info.", "noreply.", "no-reply.",
        "reply.", "news.", "hello.", "support.", "notifications.",
        "newsletter.", "service.",
    ):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
            break
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


# ══════════════════════════════════════════════════════════════════════════════
# HubSpot API
# ══════════════════════════════════════════════════════════════════════════════

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the contact dict or None."""
    url     = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    r = requests.post(url, headers=_hs_headers(), json=payload, timeout=15)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(props: dict) -> dict:
    """Create a new HubSpot contact. Returns the created contact dict."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
    r   = requests.post(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    r.raise_for_status()
    return r.json()


def hs_update_contact(contact_id: str, props: dict) -> dict:
    """Patch an existing HubSpot contact with the given properties."""
    url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
    r   = requests.patch(url, headers=_hs_headers(), json={"properties": props}, timeout=15)
    r.raise_for_status()
    return r.json()


def hs_create_note(contact_id: str, body: str, timestamp_ms: int):
    """
    Create a HubSpot NOTE activity and associate it with the contact.
    Association type 202 = HUBSPOT_DEFINED note → contact.
    """
    # Step 1: create the note object
    note_url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
    r = requests.post(
        note_url,
        headers=_hs_headers(),
        json={"properties": {
            "hs_note_body": body,
            "hs_timestamp": str(timestamp_ms),
        }},
        timeout=15,
    )
    r.raise_for_status()
    note_id = r.json()["id"]

    # Step 2: associate note → contact
    assoc_url = (
        f"{HUBSPOT_BASE}/crm/v4/objects/notes/{note_id}"
        f"/associations/contacts/{contact_id}"
    )
    r2 = requests.put(
        assoc_url,
        headers=_hs_headers(),
        json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        timeout=15,
    )
    r2.raise_for_status()


# ══════════════════════════════════════════════════════════════════════════════
# Core sync logic
# ══════════════════════════════════════════════════════════════════════════════

def process_message(msg: dict) -> dict:
    """
    Process one Gmail message and sync the sender to HubSpot.

    Returns a result dict:
        status     : "Creato" | "Aggiornato" | "Ignorato"
        email      : sender email address
        contact_id : HubSpot contact ID or None
        reason     : explanation when status is "Ignorato"
    """
    from_header = msg["from"]
    subject     = msg["subject"]
    ts_ms       = msg["internal_date_ms"]

    sender_email, firstname, lastname = parse_sender(from_header)

    # ── Guards ────────────────────────────────────────────────────────────────
    if not sender_email or "@" not in sender_email:
        return {
            "status": "Ignorato",
            "email": from_header or "(vuoto)",
            "contact_id": None,
            "reason": "indirizzo email non valido",
        }

    domain = sender_email.split("@")[1]

    if MY_EMAIL and sender_email == MY_EMAIL:
        return {"status": "Ignorato", "email": sender_email, "contact_id": None, "reason": "email propria"}

    if domain in SKIP_DOMAINS:
        return {
            "status": "Ignorato",
            "email": sender_email,
            "contact_id": None,
            "reason": f"dominio escluso ({domain})",
        }

    # ── Build contact properties ───────────────────────────────────────────────
    company = "" if domain in GENERIC_DOMAINS else domain_to_company(sender_email)

    create_props: dict = {
        "email": sender_email,
        "firstname": firstname,
        "lifecyclestage": "lead",
    }
    if lastname:
        create_props["lastname"] = lastname
    if company:
        create_props["company"] = company

    if DRY_RUN:
        log.info("[DRY RUN] %s → %s", sender_email, create_props)
        return {"status": "Creato (dry-run)", "email": sender_email, "contact_id": "N/A"}

    # ── HubSpot upsert ─────────────────────────────────────────────────────────
    existing = hs_find_contact(sender_email)

    if existing:
        contact_id = existing["id"]
        ex_props   = existing.get("properties", {})

        # Only fill in fields that are currently blank
        update_props = {
            k: v
            for k, v in {"firstname": firstname, "lastname": lastname, "company": company}.items()
            if v and not ex_props.get(k)
        }
        if update_props:
            hs_update_contact(contact_id, update_props)

        status = "Aggiornato"
    else:
        created    = hs_create_contact(create_props)
        contact_id = created["id"]
        status     = "Creato"

    # ── Timeline note ──────────────────────────────────────────────────────────
    note_body = (
        f"Email ricevuta via Gmail\n"
        f"Tag: Inbound Gmail\n"
        f"Oggetto: {subject}\n"
        f"Mittente: {from_header}\n"
        f"Fonte contatto: Gmail"
    )
    try:
        hs_create_note(contact_id, note_body, ts_ms)
    except Exception as exc:
        log.warning("Nota HubSpot non creata per %s: %s", sender_email, exc)

    return {"status": status, "email": sender_email, "contact_id": contact_id}


# ══════════════════════════════════════════════════════════════════════════════
# Main polling loop
# ══════════════════════════════════════════════════════════════════════════════

def _check_config():
    if not HUBSPOT_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN non impostato. Aggiungilo al file .env o alle variabili d'ambiente.")
        sys.exit(1)


def main():
    _check_config()

    log.info("══════════════════════════════════════════")
    log.info("  Gmail → HubSpot Contact Sync avviato")
    log.info("  Polling ogni %d secondi", POLL_INTERVAL)
    if MY_EMAIL:
        log.info("  Email propria ignorata: %s", MY_EMAIL)
    if SKIP_DOMAINS:
        log.info("  Domini esclusi: %s", ", ".join(sorted(SKIP_DOMAINS)))
    if DRY_RUN:
        log.warning("  ⚠  DRY_RUN attivo – nessuna scrittura su HubSpot")
    if RUN_ONCE:
        log.info("  Modalità: singola scansione (--once)")
    log.info("══════════════════════════════════════════")

    service = get_gmail_service()
    state   = load_state()

    while True:
        try:
            processed_ids = set(state.get("processed_message_ids", []))
            log.info(
                "── Scansione %s ──",
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            )

            new_msgs = fetch_new_messages(service, processed_ids)

            if new_msgs:
                log.info("  %d nuove email da processare.", len(new_msgs))
            else:
                log.info("  Nessuna nuova email.")

            # ── Per ogni nuova email ───────────────────────────────────────────
            for raw_msg in new_msgs:
                msg_id  = raw_msg["id"]
                details = get_message_details(service, msg_id)
                if not details:
                    processed_ids.add(msg_id)
                    continue

                try:
                    result = process_message(details)
                except requests.HTTPError as exc:
                    body = exc.response.text if exc.response is not None else str(exc)
                    log.error("  Errore HubSpot per msg %s: %s", msg_id, body)
                    processed_ids.add(msg_id)
                    continue

                # ── Log strutturato ────────────────────────────────────────────
                status = result["status"]
                email  = result["email"]
                cid    = result.get("contact_id") or "—"
                reason = result.get("reason", "")

                if reason:
                    log.info("  %-13s  %-45s  (%s)", status, email, reason)
                else:
                    log.info("  %-13s  %-45s  ID HubSpot: %s", status, email, cid)

                processed_ids.add(msg_id)

            # ── Aggiorna stato (max 1 000 ID) ──────────────────────────────────
            state["processed_message_ids"] = list(processed_ids)[-1000:]
            save_state(state)

        except Exception as exc:
            log.error("Errore nel ciclo di polling: %s", exc, exc_info=True)

        if RUN_ONCE:
            log.info("Scansione completata (--once). Uscita.")
            break

        log.info("  Prossima scansione tra %d secondi.\n", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
