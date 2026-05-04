#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox and upserts sender contacts into HubSpot.

Environment variables (required):
  HUBSPOT_ACCESS_TOKEN    Private App token

Environment variables (optional):
  GMAIL_CREDENTIALS_FILE  Path to OAuth2 credentials JSON   (default: credentials.json)
  GMAIL_TOKEN_FILE        Path to stored token file          (default: token.json)
  POLL_INTERVAL           Seconds between inbox polls        (default: 60)
  STATE_FILE              Path to processed-IDs state file   (default: sync_state.json)
  LOG_LEVEL               Logging verbosity                  (default: INFO)

Setup:
  1. Create a Google Cloud project, enable the Gmail API, download OAuth2 credentials
     as credentials.json, and place it next to this script.
  2. Create a HubSpot Private App with scopes:
       crm.objects.contacts.read  crm.objects.contacts.write
       crm.objects.notes.read     crm.objects.notes.write
     Copy the access token into HUBSPOT_ACCESS_TOKEN.
  3. Install dependencies:  pip install -r requirements.txt
  4. Run:  python gmail_hubspot_sync.py
     (On first run a browser window will open for Gmail OAuth consent.)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HUBSPOT_BASE = "https://api.hubapi.com"

STATE_FILE = Path(os.environ.get("STATE_FILE", "sync_state.json"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
GMAIL_CREDENTIALS_FILE = Path(os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"))
GMAIL_TOKEN_FILE = Path(os.environ.get("GMAIL_TOKEN_FILE", "token.json"))

# Domains treated as personal (company name is not inferred from them)
PERSONAL_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "me.com", "msn.com", "aol.com",
    "protonmail.com", "pm.me", "tutanota.com", "fastmail.com",
    "yandex.com", "mail.com", "gmx.com", "libero.it", "alice.it",
    "virgilio.it", "tiscali.it", "tin.it", "email.it",
})

# Automated-sender patterns to skip
_SKIP_RE = re.compile(
    r"(noreply|no[-_.]reply|do[-_.]not[-_.]reply|"
    r"mailer[-_]daemon|postmaster|bounce|auto[-_.]reply|"
    r"notifications?|newsletter|info@|support@)",
    re.IGNORECASE,
)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── State management ──────────────────────────────────────────────────────────


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s — starting fresh.", STATE_FILE)
    return {"processed_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Gmail helpers ─────────────────────────────────────────────────────────────


def build_gmail_service() -> Any:
    """Authenticate with Gmail (OAuth2) and return a service client."""
    creds: Optional[Credentials] = None

    if GMAIL_TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found: {GMAIL_CREDENTIALS_FILE}\n"
                    "Download them from the Google Cloud Console and retry."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_FILE), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_FILE.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_inbox_messages(service: Any, processed_ids: set[str]) -> list[dict]:
    """Return new INBOX messages (metadata only) not already processed."""
    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", q="in:inbox -from:me", maxResults=50)
            .execute()
        )
    except HttpError as exc:
        log.error("Gmail list error: %s", exc)
        return []

    messages: list[dict] = []
    for ref in result.get("messages", []):
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
            messages.append(msg)
        except HttpError as exc:
            log.warning("Could not fetch message %s: %s", ref["id"], exc)

    return messages


def parse_sender(message: dict) -> Optional[dict]:
    """Extract and validate sender data from a Gmail message object.

    Returns None for automated senders or messages without a usable From address.
    """
    headers = {
        h["name"].lower(): h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    from_header = headers.get("from", "")
    if not from_header:
        return None

    display_name, email_addr = parseaddr(from_header)
    email_lower = email_addr.lower().strip()

    if not email_lower or "@" not in email_lower:
        return None

    if _SKIP_RE.search(email_lower) or _SKIP_RE.search(display_name):
        log.debug("Skipping automated sender: %s", email_lower)
        return None

    domain = email_lower.split("@")[1]

    firstname = lastname = ""
    name = display_name.strip()
    if name:
        # Strip surrounding quotes that some clients add
        name = name.strip('"\'')
        parts = name.split(" ", 1)
        firstname = parts[0]
        lastname = parts[1] if len(parts) > 1 else ""

    company = "" if domain in PERSONAL_EMAIL_DOMAINS else domain

    return {
        "email": email_lower,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
        "subject": headers.get("subject", "(nessun oggetto)"),
    }


# ── HubSpot helpers ───────────────────────────────────────────────────────────


def _hs_token() -> str:
    token = os.environ.get("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        raise EnvironmentError(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Create a HubSpot Private App and export the token."
        )
    return token


def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {_hs_token()}",
        "Content-Type": "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    """Search HubSpot for a contact by email. Returns the first match or None."""
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> dict:
    """Create a new HubSpot contact from sender data."""
    props: dict[str, str] = {
        "email": sender["email"],
        # EMAIL_MARKETING is the closest standard source value to 'Gmail inbound'
        "hs_analytics_source": "EMAIL_MARKETING",
        "hs_lead_status": "NEW",
    }
    if sender["firstname"]:
        props["firstname"] = sender["firstname"]
    if sender["lastname"]:
        props["lastname"] = sender["lastname"]
    if sender["company"]:
        props["company"] = sender["company"]

    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, sender: dict, existing_props: dict) -> bool:
    """Fill in missing fields on an existing contact. Returns True if updated."""
    updates: dict[str, str] = {}

    if sender["firstname"] and not existing_props.get("firstname"):
        updates["firstname"] = sender["firstname"]
    if sender["lastname"] and not existing_props.get("lastname"):
        updates["lastname"] = sender["lastname"]
    if sender["company"] and not existing_props.get("company"):
        updates["company"] = sender["company"]

    if not updates:
        return False

    resp = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=15,
    )
    resp.raise_for_status()
    return True


def hs_add_inbound_note(contact_id: str, sender: dict) -> None:
    """Attach an '[Inbound Gmail]' note to the contact's activity timeline."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"[Inbound Gmail] Email ricevuta da {sender['email']}\n"
        f"Oggetto: {sender['subject']}"
    )
    payload = {
        "properties": {
            "hs_timestamp": str(now_ms),
            "hs_note_body": body,
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        # 202 = note → contact (primary association)
                        "associationTypeId": 202,
                    }
                ],
            }
        ],
    }
    try:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            headers=_hs_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning("Could not create activity note for contact %s: %s", contact_id, exc)


# ── Core sync logic ───────────────────────────────────────────────────────────


def process_message(message: dict) -> dict:
    """Process a single Gmail message: upsert contact in HubSpot.

    Returns a result dict with keys: status, email, contact_id.
    Possible statuses: Creato | Aggiornato | Ignorato
    """
    sender = parse_sender(message)

    if not sender:
        return {
            "status": "Ignorato",
            "reason": "mittente automatico o indirizzo non valido",
            "email": None,
            "contact_id": None,
        }

    existing = hs_find_contact(sender["email"])

    if existing:
        contact_id = existing["id"]
        updated = hs_update_contact(contact_id, sender, existing.get("properties", {}))
        hs_add_inbound_note(contact_id, sender)
        return {
            "status": "Aggiornato" if updated else "Ignorato",
            "email": sender["email"],
            "contact_id": contact_id,
        }

    created = hs_create_contact(sender)
    contact_id = created["id"]
    hs_add_inbound_note(contact_id, sender)
    return {
        "status": "Creato",
        "email": sender["email"],
        "contact_id": contact_id,
    }


def run_cycle(service: Any, state: dict) -> list[dict]:
    """Fetch new messages, process them, persist state. Returns results list."""
    processed_set = set(state["processed_ids"])
    messages = fetch_inbox_messages(service, processed_set)
    results: list[dict] = []

    for msg in messages:
        msg_id = msg["id"]
        try:
            result = process_message(msg)
            state["processed_ids"].append(msg_id)
            results.append(result)

            log.info(
                "%-10s  %-42s  %s",
                result["status"],
                result.get("email") or "-",
                result.get("contact_id") or "-",
            )
        except Exception as exc:
            log.error("Errore sul messaggio %s: %s", msg_id, exc, exc_info=True)
            results.append(
                {"status": "Errore", "message_id": msg_id, "error": str(exc)}
            )

    # Keep the processed-IDs list bounded to avoid unbounded growth
    if len(state["processed_ids"]) > 10_000:
        state["processed_ids"] = state["processed_ids"][-5_000:]

    save_state(state)
    return results


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    service = build_gmail_service()
    state = load_state()

    log.info(
        "Gmail → HubSpot sync avviato. Polling ogni %d secondi.", POLL_INTERVAL
    )

    while True:
        log.info("Controllo nuove email…")
        results = run_cycle(service, state)

        synced = [r for r in results if r["status"] in ("Creato", "Aggiornato")]
        ignored = [r for r in results if r["status"] == "Ignorato"]
        errors = [r for r in results if r["status"] == "Errore"]

        if synced or errors:
            log.info(
                "Ciclo completato — Creati/Aggiornati: %d | Ignorati: %d | Errori: %d",
                len(synced),
                len(ignored),
                len(errors),
            )
        else:
            log.info("Nessuna nuova email da processare.")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
