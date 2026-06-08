#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
─────────────────────────────
Monitora la casella Gmail e sincronizza automaticamente i mittenti
come contatti in HubSpot.

Output per ogni email processata
  ✅ CREATED   | email=... | hs_id=...
  🔄 UPDATED   | email=... | hs_id=...
  ⏭ IGNORED   | email=... | hs_id=n/a | reason=...

Setup rapido
  1. cp .env.example .env  →  inserisci HUBSPOT_ACCESS_TOKEN
  2. Scarica credentials.json da Google Cloud Console (Gmail API abilitata)
  3. pip install -r requirements.txt
  4. python main.py          (al primo avvio apre il browser per il login Gmail)
"""

import logging
import os
import time

from dotenv import load_dotenv

from sync.contact_extractor import extract_sender, is_noreply, is_valid_email
from sync.gmail_client import GmailClient
from sync.hubspot_client import HubSpotClient
from sync.models import SyncResult
from sync.store import ProcessedStore

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
HUBSPOT_TOKEN = os.environ["HUBSPOT_ACCESS_TOKEN"]
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
PROCESSED_FILE = os.getenv("PROCESSED_IDS_FILE", ".processed_message_ids.json")
CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
TIMELINE_EVENT_TYPE_ID = os.getenv("HUBSPOT_TIMELINE_EVENT_TYPE_ID", "")
INBOUND_LABEL = os.getenv("GMAIL_INBOUND_LABEL", "HubSpot-Synced")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)
# ─────────────────────────────────────────────────────────────────────────────


def process_message(
    msg_id: str,
    gmail: GmailClient,
    hs: HubSpotClient,
) -> SyncResult:
    from_header = gmail.get_from_header(msg_id)
    if not from_header:
        return SyncResult(status="ignored", contact_email="?", reason="no_from_header")

    sender = extract_sender(from_header)

    if not is_valid_email(sender.email):
        return SyncResult(status="ignored", contact_email=from_header, reason="invalid_email")

    if is_noreply(sender.email):
        return SyncResult(status="ignored", contact_email=sender.email, reason="noreply_address")

    existing = hs.find_contact_by_email(sender.email)

    if existing:
        contact_id = existing["id"]
        updated = hs.update_contact_if_needed(contact_id, sender)
        hs.log_email_received(contact_id, sender.email, TIMELINE_EVENT_TYPE_ID or None)
        return SyncResult(
            status="updated" if updated else "ignored",
            contact_email=sender.email,
            hubspot_id=contact_id,
            reason=None if updated else "no_fields_to_update",
        )
    else:
        new_id = hs.create_contact(sender)
        if new_id:
            hs.log_email_received(new_id, sender.email, TIMELINE_EVENT_TYPE_ID or None)
            return SyncResult(status="created", contact_email=sender.email, hubspot_id=new_id)
        return SyncResult(status="ignored", contact_email=sender.email, reason="create_failed")


def main() -> None:
    log.info("=== Gmail → HubSpot Contact Sync avviato ===")
    log.info("Polling ogni %ds | Label Gmail: '%s'", POLL_INTERVAL, INBOUND_LABEL)

    gmail = GmailClient(CREDENTIALS_FILE, TOKEN_FILE)
    hs = HubSpotClient(HUBSPOT_TOKEN)
    store = ProcessedStore(PROCESSED_FILE)
    synced_label_id = gmail.get_or_create_label(INBOUND_LABEL)

    log.info("Messaggi già processati in archivio: %d", len(store))

    while True:
        log.info("── Polling Gmail inbox ──")
        messages = gmail.list_inbox_messages(max_results=100)
        new_count = sum(1 for m in messages if not store.seen(m["id"]))
        log.info("Messaggi trovati: %d | Nuovi: %d", len(messages), new_count)

        for msg in messages:
            msg_id = msg["id"]
            if store.seen(msg_id):
                continue

            result = process_message(msg_id, gmail, hs)
            store.mark(msg_id)

            log.info(result.log_line())

            if synced_label_id and result.status in ("created", "updated"):
                gmail.add_label(msg_id, synced_label_id)

        log.info("Prossimo poll tra %ds...", POLL_INTERVAL)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
