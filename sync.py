#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and syncs sender contacts to HubSpot CRM.

Usage:
    python sync.py            # continuous loop (default)
    python sync.py --once     # run one pass and exit
    python sync.py --dry-run  # run without writing to HubSpot
"""

import argparse
import json
import logging
import os
import sys
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from hubspot import HubSpot
from hubspot.crm.contacts.exceptions import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.engagements.models import (
    SimplePublicObjectInputForCreate as EngagementCreate,
)

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = Path(os.getenv("STATE_FILE", "sync_state.json"))
CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Senders to skip (no-reply / automated mailers)
_SKIP_PREFIXES = ("noreply", "no-reply", "mailer-daemon", "postmaster",
                  "bounce", "notifications", "donotreply", "do-not-reply")
_SKIP_DOMAINS = {
    "gmail.com", "googlemail.com",
    "noreply.google.com", "accounts.google.com",
    "notifications.google.com",
}

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {"history_id": None, "processed_ids": []}


def save_state(state: dict) -> None:
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


# ── Gmail auth ─────────────────────────────────────────────────────────────────

def get_gmail_service():
    creds = None
    token_path = Path(TOKEN_FILE)

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not Path(CREDENTIALS_FILE).exists():
                log.error(
                    "File '%s' non trovato. Scarica le credenziali OAuth da "
                    "Google Cloud Console e salvale come '%s'.",
                    CREDENTIALS_FILE, CREDENTIALS_FILE,
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, "w") as fh:
            fh.write(creds.to_json())
        log.info("Token Gmail salvato in %s", TOKEN_FILE)

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


# ── HubSpot client ─────────────────────────────────────────────────────────────

def get_hubspot_client() -> HubSpot:
    if not HUBSPOT_ACCESS_TOKEN:
        log.error("HUBSPOT_ACCESS_TOKEN non configurato in .env")
        sys.exit(1)
    return HubSpot(access_token=HUBSPOT_ACCESS_TOKEN)


# ── Sender parsing ─────────────────────────────────────────────────────────────

def parse_sender(from_header: str) -> Optional[dict]:
    """Return dict with email/first_name/last_name/company/domain or None."""
    display_name, email_addr = parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    local, domain = email_addr.split("@", 1)

    # Skip automated senders
    if domain in _SKIP_DOMAINS:
        return None
    if any(local.startswith(p) for p in _SKIP_PREFIXES):
        return None

    # Parse name parts
    first_name, last_name = "", ""
    if display_name:
        parts = display_name.strip().split(None, 1)
        first_name = parts[0] if parts else ""
        last_name = parts[1] if len(parts) > 1 else ""

    # Derive company name from domain root (e.g. acme.com → Acme)
    root = domain.split(".")[0]
    company = root.replace("-", " ").replace("_", " ").title()
    if company.lower() in ("mail", "email", "smtp", "mx", "info", "support"):
        company = ""

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
    }


# ── HubSpot operations ─────────────────────────────────────────────────────────

def find_hubspot_contact(client: HubSpot, email: str):
    """Return the first matching contact or None."""
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(
                        filters=[
                            Filter(
                                property_name="email",
                                operator="EQ",
                                value=email,
                            )
                        ]
                    )
                ],
                properties=["email", "firstname", "lastname", "company",
                            "lead_source", "hs_analytics_source"],
                limit=1,
            )
        )
        return result.results[0] if result.total > 0 else None
    except ApiException as exc:
        log.error("HubSpot search failed: %s", exc)
        return None


def create_hubspot_contact(client: HubSpot, info: dict) -> Optional[str]:
    props = _build_props(info, existing=None)
    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return contact.id
    except ApiException as exc:
        log.error("HubSpot create failed for %s: %s", info["email"], exc)
        return None


def update_hubspot_contact(client: HubSpot, contact_id: str,
                            existing_contact, info: dict) -> bool:
    """Update only fields that are currently empty."""
    props = _build_props(info, existing=existing_contact.properties)
    if not props:
        return False
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=props),
        )
        return True
    except ApiException as exc:
        log.error("HubSpot update failed for %s: %s", info["email"], exc)
        return False


def _build_props(info: dict, existing: Optional[dict]) -> dict:
    """Return only the properties that need to be set (skip already filled)."""
    candidates = {}

    if info.get("first_name"):
        candidates["firstname"] = info["first_name"]
    if info.get("last_name"):
        candidates["lastname"] = info["last_name"]
    if info.get("company"):
        candidates["company"] = info["company"]
    candidates["lead_source"] = "OTHER"  # closest standard HubSpot value

    if existing is None:
        # Creating – include email plus everything non-empty
        candidates["email"] = info["email"]
        return candidates

    # Updating – include only fields not yet set
    return {
        k: v for k, v in candidates.items()
        if not existing.get(k)
    }


def add_note_to_contact(client: HubSpot, contact_id: str, subject: str,
                         dry_run: bool = False) -> None:
    """Create a HubSpot Note engagement linked to the contact."""
    if dry_run:
        return
    body = f"Email ricevuta via Gmail\nOggetto: {subject}\nTag: Inbound Gmail"
    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                }
            )
        )
        # Associate note with contact
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception as exc:
        log.debug("Note creation skipped: %s", exc)


# ── Gmail fetch logic ──────────────────────────────────────────────────────────

def fetch_new_message_ids(gmail_svc, state: dict) -> list[str]:
    """Use Gmail History API when possible; fall back to unread-inbox query."""
    ids: list[str] = []

    if state.get("history_id"):
        try:
            history_resp = (
                gmail_svc.users()
                .history()
                .list(
                    userId="me",
                    startHistoryId=state["history_id"],
                    historyTypes=["messageAdded"],
                    labelId="INBOX",
                )
                .execute()
            )
            for record in history_resp.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg = added["message"]
                    if "INBOX" in msg.get("labelIds", []):
                        ids.append(msg["id"])

            if "historyId" in history_resp:
                state["history_id"] = history_resp["historyId"]
            return ids

        except HttpError as exc:
            if exc.resp.status == 404:
                log.warning("History ID scaduto, ripristino con scansione completa")
                state["history_id"] = None
            else:
                raise

    # Bootstrap: record current historyId and pull recent unread inbox messages
    profile = gmail_svc.users().getProfile(userId="me").execute()
    state["history_id"] = profile["historyId"]
    log.info("History ID inizializzato: %s", state["history_id"])

    result = (
        gmail_svc.users()
        .messages()
        .list(userId="me", labelIds=["INBOX"], q="is:unread newer_than:1d",
              maxResults=100)
        .execute()
    )
    return [m["id"] for m in result.get("messages", [])]


def get_from_header(gmail_svc, msg_id: str) -> tuple[str, str]:
    """Return (From header, Subject header) for a message ID."""
    msg = (
        gmail_svc.users()
        .messages()
        .get(userId="me", id=msg_id, format="metadata",
             metadataHeaders=["From", "Subject"])
        .execute()
    )
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    return headers.get("From", ""), headers.get("Subject", "(senza oggetto)")


# ── Core processing ────────────────────────────────────────────────────────────

def process_message(gmail_svc, hs_client: HubSpot, msg_id: str,
                    processed_ids: set, dry_run: bool) -> dict:
    if msg_id in processed_ids:
        return {"status": "Ignorato", "reason": "già processato", "msg_id": msg_id}

    try:
        from_header, subject = get_from_header(gmail_svc, msg_id)
    except HttpError as exc:
        log.error("Errore lettura messaggio %s: %s", msg_id, exc)
        return {"status": "Errore", "msg_id": msg_id}

    info = parse_sender(from_header)
    if info is None:
        return {
            "status": "Ignorato",
            "reason": "mittente automatico/sistema",
            "from": from_header,
        }

    email = info["email"]

    if dry_run:
        existing = find_hubspot_contact(hs_client, email)
        contact_id = existing.id if existing else "DRY-RUN-ID"
        status = "[DRY-RUN] Aggiornato" if existing else "[DRY-RUN] Creato"
        log.info("[DRY-RUN] %-32s │ %s │ ID: %s │ %s",
                 email, status, contact_id, subject[:40])
        return {"status": status, "email": email, "contact_id": contact_id}

    existing = find_hubspot_contact(hs_client, email)

    if existing:
        changed = update_hubspot_contact(hs_client, existing.id, existing, info)
        contact_id = existing.id
        status = "Aggiornato" if changed else "Ignorato (già completo)"
        if changed:
            add_note_to_contact(hs_client, contact_id, subject)
    else:
        contact_id = create_hubspot_contact(hs_client, info)
        status = "Creato" if contact_id else "Errore creazione"
        if contact_id:
            add_note_to_contact(hs_client, contact_id, subject)

    log.info("%-32s │ %-22s │ ID: %-10s │ %s",
             email, status, contact_id or "—", subject[:40])

    return {"status": status, "email": email, "contact_id": contact_id}


# ── Entry point ────────────────────────────────────────────────────────────────

def run(once: bool = False, dry_run: bool = False) -> None:
    log.info("=== Gmail → HubSpot Sync avviato%s ===",
             " [DRY-RUN]" if dry_run else "")

    gmail_svc = get_gmail_service()
    hs_client = get_hubspot_client()
    state = load_state()
    processed_ids: set[str] = set(state.get("processed_ids", []))

    while True:
        try:
            log.info("Controllo nuove email…")
            new_ids = fetch_new_message_ids(gmail_svc, state)
            log.info("%d messaggi trovati", len(new_ids))

            for msg_id in new_ids:
                result = process_message(
                    gmail_svc, hs_client, msg_id, processed_ids, dry_run
                )
                if result.get("status") not in (
                    "Ignorato", "Ignorato (già completo)", "Errore"
                ):
                    processed_ids.add(msg_id)

            # Bound processed-IDs list to last 20 000 entries
            state["processed_ids"] = list(processed_ids)[-20_000:]
            save_state(state)

        except Exception as exc:
            log.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        if once:
            log.info("=== Esecuzione singola completata ===")
            break

        log.info("Prossimo controllo tra %ds…", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true",
                        help="Esegui un solo ciclo e termina")
    parser.add_argument("--dry-run", action="store_true",
                        help="Leggi Gmail e HubSpot ma non scrivere nulla")
    args = parser.parse_args()
    run(once=args.once, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
