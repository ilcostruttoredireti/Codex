#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox continuously and syncs senders as HubSpot contacts.

Setup:
  1. pip install -r requirements.txt
  2. Create OAuth credentials at console.cloud.google.com → APIs → Gmail API
     Download as credentials.json in this directory
  3. Set HUBSPOT_API_KEY env var (Private App token from HubSpot settings)
  4. python gmail_hubspot_sync.py            # daemon mode
     python gmail_hubspot_sync.py --once     # single run
"""

import os
import json
import time
import logging
import re
import argparse
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import requests
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Configuration ─────────────────────────────────────────────────────────────
HUBSPOT_API_KEY      = os.getenv("HUBSPOT_API_KEY", "")
CREDENTIALS_FILE     = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE           = os.getenv("GMAIL_TOKEN_FILE", "token.json")
STATE_FILE           = os.getenv("STATE_FILE", ".sync_state.json")
POLL_INTERVAL        = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOG_LEVEL            = os.getenv("LOG_LEVEL", "INFO")

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
HS_BASE      = "https://api.hubapi.com"

# Domains treated as personal / generic (no company derived from them)
GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "live.com", "icloud.com", "me.com",
    "libero.it", "virgilio.it", "tiscali.it", "tin.it", "alice.it",
    "fastwebnet.it", "protonmail.com", "pm.me",
}

# Local-part prefixes that signal automated senders — skip these
AUTOMATED_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notification", "notifications", "alert", "alerts",
    "newsletter", "news", "unsubscribe",
)

# TLDs to strip when deriving a company name from the domain
_TLD_RE = re.compile(
    r"\.(com|it|org|net|io|co|eu|uk|de|fr|es|nl|be|ch|at|pl|ru|jp|br|au|ca|mx|biz|info)(\.\w{2})?$"
)
_SUBDOMAIN_RE = re.compile(r"^(www|mail|info|press|ufficiostampa|news|redazione)\.")

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Gmail authentication ──────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    if Path(TOKEN_FILE).exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                raise FileNotFoundError(
                    f"OAuth credentials not found: {CREDENTIALS_FILE}\n"
                    "Download from Google Cloud Console → APIs & Services → Credentials."
                )
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── State persistence ─────────────────────────────────────────────────────────

def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"history_id": None, "processed_ids": []}


def save_state(state: dict):
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── Sender parsing ────────────────────────────────────────────────────────────

def parse_sender(raw_from: str) -> Optional[dict]:
    """
    Parse RFC 2822 From header into structured contact fields.
    Returns None for automated senders or invalid addresses.
    """
    display_name, email = parseaddr(raw_from)
    email = email.lower().strip()

    if not email or "@" not in email:
        return None

    local, domain = email.split("@", 1)

    if any(local.startswith(p) for p in AUTOMATED_PREFIXES):
        return None

    # Parse first / last name from display name
    firstname, lastname = "", ""
    name = display_name.strip().strip('"')
    if name:
        # Discard names that look like company strings (ALL CAPS or > 4 words)
        parts = name.split()
        if len(parts) <= 4 and not name.isupper():
            firstname = parts[0].capitalize()
            if len(parts) > 1:
                lastname = " ".join(p.capitalize() for p in parts[1:])

    # Derive company from domain
    company = _company_from_domain(domain)

    return {
        "email":     email,
        "firstname": firstname,
        "lastname":  lastname,
        "company":   company,
        "domain":    domain,
    }


def _company_from_domain(domain: str) -> str:
    if domain in GENERIC_DOMAINS:
        return ""
    base = _SUBDOMAIN_RE.sub("", domain)
    base = _TLD_RE.sub("", base)
    # "modena-skateboard-school" → "Modena Skateboard School"
    return base.replace("-", " ").replace(".", " ").title()


# ── HubSpot API helpers ───────────────────────────────────────────────────────

def _hs_headers() -> dict:
    return {
        "Authorization": f"Bearer {HUBSPOT_API_KEY}",
        "Content-Type":  "application/json",
    }


def hs_find_contact(email: str) -> Optional[dict]:
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts/search",
        headers=_hs_headers(),
        json={
            "filterGroups": [{
                "filters": [{"propertyName": "email", "operator": "EQ", "value": email}]
            }],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        },
        timeout=15,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def hs_create_contact(sender: dict) -> dict:
    props = {
        "email":         sender["email"],
        "lifecyclestage": "lead",
        "hs_lead_status": "NEW",
    }
    if sender.get("firstname"):
        props["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        props["lastname"]  = sender["lastname"]
    if sender.get("company"):
        props["company"]   = sender["company"]

    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/contacts",
        headers=_hs_headers(),
        json={"properties": props},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def hs_update_contact(contact_id: str, existing_props: dict, sender: dict) -> bool:
    """Update only fields that are currently empty. Returns True if any update was made."""
    updates = {}
    for field in ("firstname", "lastname", "company"):
        if not existing_props.get(field) and sender.get(field):
            updates[field] = sender[field]

    if not updates:
        return False

    resp = requests.patch(
        f"{HS_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_hs_headers(),
        json={"properties": updates},
        timeout=15,
    )
    resp.raise_for_status()
    return True


def hs_add_note(contact_id: str, subject: str, sender_email: str):
    """Add an email-received activity note with the Inbound Gmail tag."""
    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"📩 Email ricevuta via Gmail\n"
        f"Da: {sender_email}\n"
        f"Oggetto: {subject}\n"
        f"Tag: Inbound Gmail\n"
        f"Fonte contatto: Gmail"
    )
    resp = requests.post(
        f"{HS_BASE}/crm/v3/objects/notes",
        headers=_hs_headers(),
        json={
            "properties": {
                "hs_note_body":  body,
                "hs_timestamp":  str(ts),
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId":   202,   # note → contact
                }],
            }],
        },
        timeout=15,
    )
    resp.raise_for_status()


# ── Core sync logic ───────────────────────────────────────────────────────────

def process_message(gmail, msg_id: str) -> dict:
    """
    Fetch a Gmail message, extract sender, sync to HubSpot.
    Returns a result dict with keys: status, email, contact_id.
    """
    full = gmail.users().messages().get(
        userId="me", id=msg_id, format="metadata",
        metadataHeaders=["From", "Subject"],
    ).execute()

    headers   = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
    raw_from  = headers.get("From", "")
    subject   = headers.get("Subject", "(no subject)")

    sender = parse_sender(raw_from)
    if not sender:
        return {"status": "Ignorato", "email": raw_from or "(empty)", "contact_id": None}

    email = sender["email"]

    existing = hs_find_contact(email)

    if existing:
        contact_id  = existing["id"]
        was_updated = hs_update_contact(contact_id, existing.get("properties", {}), sender)
        status      = "Aggiornato" if was_updated else "Già presente"
    else:
        new_contact = hs_create_contact(sender)
        contact_id  = new_contact["id"]
        status      = "Creato"

    try:
        hs_add_note(contact_id, subject, email)
    except Exception as exc:
        log.warning("Nota HubSpot non aggiunta per %s: %s", email, exc)

    return {"status": status, "email": email, "contact_id": contact_id}


# ── Gmail message fetching ────────────────────────────────────────────────────

def fetch_new_message_ids(gmail, state: dict) -> list[str]:
    """
    Return IDs of INBOX messages not yet processed.
    Uses Gmail History API for incremental fetches after the first run.
    Falls back to listing the last 24 h of messages when history expires.
    """
    processed = set(state.get("processed_ids", []))
    history_id = state.get("history_id")

    if not history_id:
        return _bootstrap_messages(gmail, state, processed)

    ids = []
    page_token = None

    try:
        while True:
            kwargs: dict = {
                "userId":         "me",
                "startHistoryId": history_id,
                "historyTypes":   ["messageAdded"],
                "labelId":        "INBOX",
            }
            if page_token:
                kwargs["pageToken"] = page_token

            resp = gmail.users().history().list(**kwargs).execute()

            for record in resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    m = added.get("message", {})
                    mid = m.get("id")
                    if mid and mid not in processed and "INBOX" in m.get("labelIds", []):
                        ids.append(mid)

            state["history_id"] = resp.get("historyId", history_id)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    except HttpError as exc:
        # history_id too old → fall back
        if exc.resp.status == 404:
            log.warning("History ID scaduto, recupero ultimi messaggi dell'ultimo giorno...")
            return _bootstrap_messages(gmail, state, processed)
        raise

    return ids


def _bootstrap_messages(gmail, state: dict, processed: set) -> list[str]:
    """Fetch message IDs from the last 24 hours as a first-run baseline."""
    result = gmail.users().messages().list(
        userId="me", labelIds=["INBOX"], q="newer_than:1d", maxResults=200
    ).execute()
    ids = [m["id"] for m in result.get("messages", []) if m["id"] not in processed]

    profile = gmail.users().getProfile(userId="me").execute()
    state["history_id"] = profile.get("historyId")
    return ids


# ── Main loop ─────────────────────────────────────────────────────────────────

_STATUS_ICON = {"Creato": "✅", "Aggiornato": "🔄", "Già presente": "–", "Ignorato": "○"}


def run_cycle(gmail, state: dict):
    ids = fetch_new_message_ids(gmail, state)

    if not ids:
        log.info("Nessuna nuova email.")
        return

    log.info("Trovate %d nuove email da processare.", len(ids))
    processed = set(state.get("processed_ids", []))

    for mid in ids:
        try:
            res = process_message(gmail, mid)
            icon  = _STATUS_ICON.get(res["status"], "?")
            cid   = res["contact_id"] or "-"
            print(f"  {icon} {res['status']:<14}  {res['email']:<48}  ID HubSpot: {cid}")
            log.info("[%s] %s → %s", res["status"], res["email"], cid)
        except Exception as exc:
            log.error("Errore su messaggio %s: %s", mid, exc)
        finally:
            processed.add(mid)

    # Keep at most last 10 000 IDs to bound state file growth
    state["processed_ids"] = list(processed)[-10_000:]
    save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Esegui un ciclo e termina")
    args = parser.parse_args()

    if not HUBSPOT_API_KEY:
        raise SystemExit(
            "❌  Variabile d'ambiente HUBSPOT_API_KEY non impostata.\n"
            "    Crea un Private App token in HubSpot → Settings → Integrations → Private Apps."
        )

    gmail = get_gmail_service()
    state = load_state()

    banner = "Gmail → HubSpot Contact Sync"
    print(f"\n{'━' * 65}")
    print(f"  {banner}  |  {'Modalità: singolo ciclo' if args.once else f'Polling ogni {POLL_INTERVAL}s'}")
    print(f"{'━' * 65}\n")
    print(f"  {'STATO':<14}  {'EMAIL MITTENTE':<48}  ID HUBSPOT")
    print(f"  {'─'*14}  {'─'*48}  {'─'*18}\n")

    if args.once:
        run_cycle(gmail, state)
        return

    while True:
        try:
            run_cycle(gmail, state)
        except Exception as exc:
            log.error("Errore nel ciclo: %s", exc)
        log.info("Prossimo controllo tra %ds…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
