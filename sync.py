#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and syncs sender contacts to HubSpot.

Usage:
    python sync.py            # run continuous sync loop
    python sync.py --once     # process new emails once and exit
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Optional

import requests
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

load_dotenv()

# ── Configuration ─────────────────────────────────────────────────────────────

GMAIL_SCOPES        = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_CREDS_FILE    = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE    = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL       = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE          = os.getenv("STATE_FILE", "sync_state.json")

HUBSPOT_API_KEY     = os.getenv("HUBSPOT_API_KEY", "")
HUBSPOT_BASE        = "https://api.hubapi.com"

CONTACT_SOURCE_TAG  = "Inbound Gmail"

# Senders whose local-part matches these prefixes are skipped (automated mail)
_AUTOMATED_PREFIXES = (
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "notifications", "newsletter",
    "support", "info", "hello", "contact",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail helpers ─────────────────────────────────────────────────────────────

def gmail_service():
    creds: Optional[Credentials] = None

    if os.path.exists(GMAIL_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GMAIL_TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(GMAIL_CREDS_FILE):
                raise FileNotFoundError(
                    f"Gmail credentials not found: {GMAIL_CREDS_FILE}\n"
                    "Run  python setup_gmail.py  first to authorise."
                )
            flow = InstalledAppFlow.from_client_secrets_file(GMAIL_CREDS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(GMAIL_TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _header(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    return next((h["value"] for h in headers if h["name"].lower() == name_lower), "")


def _parse_sender(headers: list[dict]) -> tuple[str, str]:
    """Return (email_addr, display_name) from the From header."""
    raw = _header(headers, "From")
    name, addr = parseaddr(raw)
    return addr.strip().lower(), name.strip()


def _is_automated(email: str) -> bool:
    local = email.split("@")[0] if "@" in email else email
    return local.startswith(_AUTOMATED_PREFIXES)


def fetch_new_messages(svc, history_id: Optional[str], since_epoch: int) -> tuple[list[dict], str]:
    """
    Returns (message_detail_list, new_history_id).
    Uses the History API when a history_id is available, otherwise falls back
    to a time-based query so the first run always works.
    """
    msg_ids: list[str] = []
    new_hid = history_id

    if history_id:
        try:
            resp = (
                svc.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=history_id,
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            new_hid = resp.get("historyId", history_id)
            for record in resp.get("history", []):
                for m in record.get("messagesAdded", []):
                    msg_ids.append(m["message"]["id"])
        except HttpError as exc:
            if exc.status_code == 404:
                log.warning("historyId scaduto, ripristino con query temporale")
                history_id = None
            else:
                raise

    if not history_id:
        results = (
            svc.users()
            .messages()
            .list(userId="me", labelIds=["INBOX"], q=f"after:{since_epoch}")
            .execute()
        )
        msg_ids = [m["id"] for m in results.get("messages", [])]
        profile = svc.users().getProfile(userId="me").execute()
        new_hid = profile.get("historyId", new_hid)

    details: list[dict] = []
    for mid in msg_ids:
        try:
            msg = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
            details.append(msg)
        except HttpError as exc:
            log.warning(f"Impossibile recuperare messaggio {mid}: {exc}")

    return details, new_hid


# ── HubSpot helpers ───────────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {"Authorization": f"Bearer {HUBSPOT_API_KEY}", "Content-Type": "application/json"}


def hs_search_contact(email: str) -> Optional[dict]:
    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_analytics_source_data_1"],
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def _split_name(display_name: str) -> tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    # Drop the TLD, capitalise the SLD as a company name hint
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return domain.capitalize()


def hs_create_contact(email: str, firstname: str, lastname: str, company: str) -> dict:
    props: dict[str, str] = {
        "email": email,
        "hs_analytics_source_data_1": CONTACT_SOURCE_TAG,
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    resp = requests.post(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(
    contact_id: str,
    existing: dict,
    firstname: str,
    lastname: str,
    company: str,
) -> bool:
    """Fill only empty fields. Returns True if any update was sent."""
    props: dict[str, str] = {}
    ep = existing.get("properties", {})

    if firstname and not ep.get("firstname"):
        props["firstname"] = firstname
    if lastname and not ep.get("lastname"):
        props["lastname"] = lastname
    if company and not ep.get("company"):
        props["company"] = company
    if not ep.get("hs_analytics_source_data_1"):
        props["hs_analytics_source_data_1"] = CONTACT_SOURCE_TAG

    if not props:
        return False

    resp = requests.patch(
        f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return True


def hs_add_note(contact_id: str, subject: str, sender_email: str) -> None:
    """Create a Note activity on the contact timeline."""
    note_body = (
        f"Email ricevuta da: {sender_email}\n"
        f"Oggetto: {subject}\n"
        f"Fonte: Gmail  |  Tag: {CONTACT_SOURCE_TAG}"
    )
    ts_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))

    payload = {
        "properties": {
            "hs_note_body": note_body,
            "hs_timestamp": ts_ms,
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
    try:
        resp = requests.post(
            f"{HUBSPOT_BASE}/crm/v3/objects/notes",
            headers=_hs_headers(),
            json=payload,
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.warning(f"Nota non creata per contatto {contact_id}: {exc}")


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh)


# ── Core processing ───────────────────────────────────────────────────────────

_STATUS_ICON = {
    "CREATO":    "✅",
    "AGGIORNATO": "🔄",
    "INVARIATO": "⏸ ",
    "IGNORATO":  "⏭ ",
}


def process_message(msg: dict) -> dict:
    """
    Process one Gmail message.
    Returns {"email", "status", "hubspot_id"}.
    """
    headers = msg.get("payload", {}).get("headers", [])
    sender_email, display_name = _parse_sender(headers)
    subject = _header(headers, "Subject") or "(nessun oggetto)"

    result = {"email": sender_email, "status": "IGNORATO", "hubspot_id": None}

    if not sender_email or "@" not in sender_email:
        result["status"] = "IGNORATO (email non valida)"
        return result

    if _is_automated(sender_email):
        result["status"] = "IGNORATO (mittente automatico)"
        return result

    domain = sender_email.split("@")[1]
    firstname, lastname = _split_name(display_name)
    company = _company_from_domain(domain)

    existing = hs_search_contact(sender_email)

    if existing:
        contact_id = existing["id"]
        updated = hs_update_contact(contact_id, existing, firstname, lastname, company)
        hs_add_note(contact_id, subject, sender_email)
        result["status"] = "AGGIORNATO" if updated else "INVARIATO"
        result["hubspot_id"] = contact_id
    else:
        created = hs_create_contact(sender_email, firstname, lastname, company)
        contact_id = created["id"]
        hs_add_note(contact_id, subject, sender_email)
        result["status"] = "CREATO"
        result["hubspot_id"] = contact_id

    return result


def _log_result(result: dict) -> None:
    status = result["status"].split(" ")[0]          # strip parenthetical notes
    icon = _STATUS_ICON.get(status, "❓")
    log.info(
        f"{icon} {result['status']:<25}  "
        f"email={result['email']:<40}  "
        f"HubSpot ID={result['hubspot_id'] or 'N/A'}"
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def run(once: bool = False) -> None:
    if not HUBSPOT_API_KEY:
        raise SystemExit("HUBSPOT_API_KEY non impostata. Controlla il file .env.")

    log.info("▶  Gmail → HubSpot Contact Sync avviato")
    svc = gmail_service()
    state = load_state()

    history_id: Optional[str] = state.get("history_id")
    # Default: look back 1 hour on first run
    last_epoch: int = state.get("last_check_epoch", int(time.time()) - 3600)

    while True:
        now = int(time.time())
        log.info(
            f"Controllo email nuove... (dal {datetime.fromtimestamp(last_epoch):%Y-%m-%d %H:%M:%S})"
        )

        try:
            messages, history_id = fetch_new_messages(svc, history_id, last_epoch)
            log.info(f"Trovate {len(messages)} nuove email")

            for msg in messages:
                try:
                    result = process_message(msg)
                    _log_result(result)
                except requests.HTTPError as exc:
                    log.error(f"HubSpot API error: {exc.response.status_code} — {exc.response.text}")
                except Exception as exc:
                    log.error(f"Errore processando messaggio: {exc}", exc_info=True)

            last_epoch = now
            save_state({"history_id": history_id, "last_check_epoch": last_epoch})

        except Exception as exc:
            log.error(f"Errore nel ciclo di sync: {exc}", exc_info=True)

        if once:
            log.info("Modalità --once: uscita.")
            break

        log.info(f"Prossimo controllo tra {POLL_INTERVAL}s …")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Processa una volta e poi esci")
    args = parser.parse_args()
    run(once=args.once)
