#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors Gmail inbox for incoming emails and syncs sender contacts to HubSpot CRM.
Tracks state between runs so no email is processed twice.

Usage:
  python gmail_hubspot_sync.py               # single run
  python gmail_hubspot_sync.py --continuous  # loop every 5 min (or --interval N)
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
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

# ──────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Configuration (from .env or environment variables)
# ──────────────────────────────────────────────────────────────
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
GMAIL_TOKEN_PATH = Path(os.getenv("GMAIL_TOKEN_PATH", "gmail_token.json"))
GMAIL_CREDENTIALS_PATH = Path(os.getenv("GMAIL_CREDENTIALS_PATH", "gmail_credentials.json"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
HUBSPOT_BASE = "https://api.hubapi.com"
STATE_FILE = Path("sync_state.json")
SYNC_INTERVAL = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
# Default lookback on first run (days)
FIRST_RUN_LOOKBACK_DAYS = int(os.getenv("FIRST_RUN_LOOKBACK_DAYS", "7"))
# Max messages to fetch per cycle
MAX_MESSAGES_PER_CYCLE = int(os.getenv("MAX_MESSAGES_PER_CYCLE", "100"))

# Email addresses from these domains are treated as personal (no company inferred)
PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com",
    "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "zoho.com",
    "yandex.com", "yandex.ru", "libero.it", "virgilio.it",
    "tiscali.it", "alice.it", "tin.it", "fastwebnet.it",
}

# Local-part prefixes that indicate automated/system senders
AUTOMATED_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "notification", "newsletter", "mailer-daemon",
    "postmaster", "bounce", "bounces", "automated", "auto",
    "alerts", "alert", "news", "info@", "support@",
    "admin@", "help@", "contact@", "hello@", "team@",
    "service@", "system@", "robot@", "bot@",
)


# ──────────────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────────────
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_sync_ts": 0, "processed_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ──────────────────────────────────────────────────────────────
# Gmail client
# ──────────────────────────────────────────────────────────────
def get_gmail_service():
    creds: Optional[Credentials] = None

    if GMAIL_TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(GMAIL_TOKEN_PATH), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not GMAIL_CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"File credenziali Gmail non trovato: {GMAIL_CREDENTIALS_PATH}\n"
                    "Scarica le credenziali OAuth2 da Google Cloud Console e salvale come "
                    "gmail_credentials.json (o imposta GMAIL_CREDENTIALS_PATH)."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(GMAIL_CREDENTIALS_PATH), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
        GMAIL_TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _is_automated_sender(local_part: str) -> bool:
    lp = local_part.lower()
    return any(lp == p.rstrip("@") or lp.startswith(p.rstrip("@")) for p in AUTOMATED_PREFIXES)


def extract_sender(from_header: str) -> Optional[dict]:
    """Parse a From: header into structured contact data. Returns None for auto-senders."""
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    local_part, domain = email_addr.split("@", 1)

    if _is_automated_sender(local_part):
        return None

    # Parse first / last name from display name
    first_name, last_name = "", ""
    name = display_name.strip().strip('"').strip("'")
    if name and name.lower() != email_addr:
        parts = name.split(None, 1)
        first_name = parts[0].capitalize()
        last_name = parts[1] if len(parts) > 1 else ""

    # Infer company only from business domains
    company = ""
    if domain not in PERSONAL_DOMAINS:
        raw_name = domain.split(".")[0]
        company = raw_name.replace("-", " ").replace("_", " ").title()

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
    }


def fetch_new_messages(gmail, state: dict) -> list[dict]:
    """Return list of new inbound messages not yet processed."""
    processed = set(state.get("processed_ids", []))
    last_ts = state.get("last_sync_ts", 0)

    # On first run, look back FIRST_RUN_LOOKBACK_DAYS days
    if last_ts == 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=FIRST_RUN_LOOKBACK_DAYS)
        last_ts = int(cutoff.timestamp())

    # Gmail search: inbox only, not sent by me, after timestamp
    query = f"in:inbox -in:sent after:{last_ts}"

    try:
        response = gmail.users().messages().list(
            userId="me",
            q=query,
            maxResults=MAX_MESSAGES_PER_CYCLE,
        ).execute()
    except HttpError as e:
        log.error(f"Errore Gmail API: {e}")
        return []

    raw_messages = response.get("messages", [])
    results = []

    for msg_ref in raw_messages:
        msg_id = msg_ref["id"]
        if msg_id in processed:
            continue

        try:
            msg = gmail.users().messages().get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Date", "Subject"],
            ).execute()
        except HttpError as e:
            log.warning(f"Impossibile leggere messaggio {msg_id}: {e}")
            continue

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }

        sender = extract_sender(headers.get("From", ""))
        if not sender:
            # Mark as processed so we don't re-check it
            state.setdefault("processed_ids", []).append(msg_id)
            continue

        results.append({
            "message_id": msg_id,
            "subject": headers.get("Subject", "(nessun oggetto)"),
            "date": headers.get("Date", ""),
            "internal_date_ms": int(msg.get("internalDate", str(int(time.time() * 1000)))),
            **sender,
        })

    return results


# ──────────────────────────────────────────────────────────────
# HubSpot helpers (direct REST — no SDK dependency)
# ──────────────────────────────────────────────────────────────
def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _hs_request(method: str, path: str, **kwargs) -> requests.Response:
    """Thin wrapper with automatic 429 back-off (up to 3 retries)."""
    url = f"{HUBSPOT_BASE}{path}"
    for attempt in range(4):
        resp = requests.request(method, url, headers=_hs_headers(), **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 10))
            log.warning(f"HubSpot rate limit. Attesa {retry_after}s...")
            time.sleep(retry_after)
            continue
        return resp
    return resp  # return last response even if still 429


def find_hubspot_contact(email: str) -> Optional[dict]:
    resp = _hs_request(
        "POST",
        "/crm/v3/objects/contacts/search",
        json={
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "leadsource"],
            "limit": 1,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["results"][0] if data.get("total", 0) > 0 else None


def create_hubspot_contact(sender: dict) -> Optional[str]:
    props: dict = {
        "email": sender["email"],
        "leadsource": "Gmail",
    }
    if sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if sender.get("company"):
        props["company"] = sender["company"]

    resp = _hs_request("POST", "/crm/v3/objects/contacts", json={"properties": props})

    # 409 = contact already exists (race condition or duplicate email alias)
    if resp.status_code == 409:
        log.debug(f"Contatto {sender['email']} già esistente (409) — ricerca in corso")
        existing = find_hubspot_contact(sender["email"])
        return existing["id"] if existing else None

    resp.raise_for_status()
    return resp.json()["id"]


def update_hubspot_contact(contact_id: str, sender: dict, existing_props: dict) -> bool:
    """Fill only empty fields in the existing contact. Returns True if any update was sent."""
    updates: dict = {}

    if sender.get("first_name") and not existing_props.get("firstname"):
        updates["firstname"] = sender["first_name"]
    if sender.get("last_name") and not existing_props.get("lastname"):
        updates["lastname"] = sender["last_name"]
    if sender.get("company") and not existing_props.get("company"):
        updates["company"] = sender["company"]

    if not updates:
        return False

    resp = _hs_request(
        "PATCH",
        f"/crm/v3/objects/contacts/{contact_id}",
        json={"properties": updates},
    )
    resp.raise_for_status()
    return True


def create_timeline_activity(contact_id: str, msg: dict):
    """Attach a note to the contact's HubSpot timeline for the inbound email."""
    note_body = (
        f"📥 <b>Email in entrata da Gmail</b><br>"
        f"Da: {msg['email']}<br>"
        f"Oggetto: {msg.get('subject', '')}<br>"
        f"<br>Tag: <b>Inbound Gmail</b>"
    )

    resp = _hs_request(
        "POST",
        "/crm/v3/objects/notes",
        json={
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": str(msg["internal_date_ms"]),
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
        },
    )
    if not resp.ok:
        log.warning(
            f"Nota timeline non creata per contatto {contact_id}: "
            f"{resp.status_code} {resp.text[:120]}"
        )


# ──────────────────────────────────────────────────────────────
# Per-message processing
# ──────────────────────────────────────────────────────────────
def process_message(msg: dict) -> dict:
    email = msg["email"]
    result = {"stato": "Errore", "email": email, "hubspot_id": "N/A"}

    try:
        existing = find_hubspot_contact(email)

        if existing:
            contact_id = existing["id"]
            existing_props = existing.get("properties", {})
            updated = update_hubspot_contact(contact_id, msg, existing_props)
            result["stato"] = "Aggiornato" if updated else "Ignorato"
            result["hubspot_id"] = contact_id
        else:
            contact_id = create_hubspot_contact(msg)
            if contact_id:
                create_timeline_activity(contact_id, msg)
                result["stato"] = "Creato"
                result["hubspot_id"] = contact_id
            else:
                result["stato"] = "Errore (ID non restituito)"

    except requests.HTTPError as exc:
        body = exc.response.text[:200] if exc.response is not None else str(exc)
        log.error(f"HTTP error per {email}: {exc.response.status_code if exc.response else '?'} — {body}")
        result["stato"] = f"Errore HTTP {exc.response.status_code if exc.response else ''}"
    except Exception as exc:
        log.error(f"Errore inatteso per {email}: {exc}", exc_info=True)
        result["stato"] = f"Errore: {type(exc).__name__}"

    return result


# ──────────────────────────────────────────────────────────────
# Main sync loop
# ──────────────────────────────────────────────────────────────
def run_sync(continuous: bool = False, interval: int = SYNC_INTERVAL):
    if not HUBSPOT_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN non configurato.\n"
            "Aggiungilo al file .env o come variabile d'ambiente."
        )

    gmail = get_gmail_service()
    state = load_state()

    log.info("═" * 56)
    log.info("  Gmail → HubSpot Contact Sync  —  avviato")
    log.info("═" * 56)

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Ciclo #{cycle}  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ──")

        messages = fetch_new_messages(gmail, state)
        log.info(f"Nuove email da processare: {len(messages)}")

        summary = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}

        for msg in messages:
            result = process_message(msg)
            stato = result["stato"]

            # Bucket generic errors
            bucket = stato if stato in summary else "Errore"
            summary[bucket] += 1

            log.info(
                f"  [{stato:<10}]  {result['email']:<40}  "
                f"ID HubSpot: {result['hubspot_id']}"
            )

            # Mark as processed regardless of outcome to avoid infinite retries
            state.setdefault("processed_ids", []).append(msg["message_id"])

        # Update timestamp and trim state list
        state["last_sync_ts"] = int(time.time())
        state["processed_ids"] = state["processed_ids"][-2000:]
        save_state(state)

        log.info(
            f"Riepilogo: {summary['Creato']} creati | "
            f"{summary['Aggiornato']} aggiornati | "
            f"{summary['Ignorato']} ignorati | "
            f"{summary['Errore']} errori"
        )

        if not continuous:
            break

        log.info(f"Prossima sync tra {interval}s  (Ctrl+C per interrompere)\n")
        time.sleep(interval)


# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Gmail → HubSpot Contact Sync",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Modalità continua: ripete la sync ogni --interval secondi",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=SYNC_INTERVAL,
        help="Secondi tra una sync e l'altra",
    )
    args = parser.parse_args()

    run_sync(continuous=args.continuous, interval=args.interval)
