#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Continuously monitors the Gmail inbox and upserts every new sender as a
HubSpot contact, avoiding duplicates and filling in missing fields.

Usage:
    python sync.py              # run the polling loop
    python sync.py --once       # single pass then exit (useful for cron)

Environment (see .env.example):
    HUBSPOT_ACCESS_TOKEN   — HubSpot Private App token (required)
    GMAIL_CREDENTIALS_FILE — path to credentials.json (default: ./credentials.json)
    MY_EMAIL               — your own address, skipped as sender
    POLL_INTERVAL_SECONDS  — seconds between checks (default: 60)
    HUBSPOT_CREATE_ACTIVITY — 'true' to add a timeline note per email (default: false)
"""

import logging
import os
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Set

from dotenv import load_dotenv

load_dotenv()

import gmail_client
import hubspot_client
from contact_parser import ContactData, parse_sender
from state import get_history_id, set_history_id

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MY_EMAIL: str = os.getenv("MY_EMAIL", "").lower().strip()
CREATE_ACTIVITY: bool = os.getenv("HUBSPOT_CREATE_ACTIVITY", "false").lower() == "true"
HUBSPOT_TOKEN: str = os.getenv("HUBSPOT_ACCESS_TOKEN", "")


# ── Result model ─────────────────────────────────────────────────────────────


class Status(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: Status
    email: str
    contact_id: Optional[str] = None


# ── Core logic ───────────────────────────────────────────────────────────────


def sync_contact(hs, contact: ContactData) -> SyncResult:
    """Upsert a single contact in HubSpot and return the outcome."""
    existing = hubspot_client.find_by_email(hs, contact.email)

    if existing:
        updated = hubspot_client.update_missing_fields(
            hs,
            existing.id,
            existing.properties,
            contact.first_name,
            contact.last_name,
            contact.company,
        )
        if CREATE_ACTIVITY and HUBSPOT_TOKEN:
            hubspot_client.create_activity_note(HUBSPOT_TOKEN, existing.id, contact.email)

        status = Status.UPDATED if updated else Status.IGNORED
        return SyncResult(status=status, email=contact.email, contact_id=existing.id)

    contact_id = hubspot_client.create_contact(
        hs,
        contact.email,
        contact.first_name,
        contact.last_name,
        contact.company,
    )
    if contact_id:
        if CREATE_ACTIVITY and HUBSPOT_TOKEN:
            hubspot_client.create_activity_note(HUBSPOT_TOKEN, contact_id, contact.email)
        return SyncResult(status=Status.CREATED, email=contact.email, contact_id=contact_id)

    return SyncResult(status=Status.ERROR, email=contact.email)


def run_cycle(gmail, hs, seen: Set[str]) -> Set[str]:
    """Fetch new inbox messages, extract senders, sync each to HubSpot.

    `seen` is an in-memory set of email addresses already processed this
    session, used to deduplicate multiple messages from the same sender
    within a single run without hitting HubSpot's search API repeatedly.
    """
    history_id = get_history_id()
    message_ids = gmail_client.get_inbox_message_ids(gmail, after_history_id=history_id)

    processed = 0
    for msg_id in message_ids:
        from_header = gmail_client.get_from_header(gmail, msg_id)
        if not from_header:
            continue

        contact = parse_sender(from_header)
        if contact is None:
            continue
        if MY_EMAIL and contact.email == MY_EMAIL:
            continue
        if contact.email in seen:
            continue

        seen.add(contact.email)
        result = sync_contact(hs, contact)
        processed += 1

        logger.info(
            "[%-9s]  %-45s  ID: %s",
            result.status.value,
            result.email,
            result.contact_id or "N/A",
        )

    # Advance the history cursor regardless of how many messages were processed
    new_history_id = gmail_client.get_profile_history_id(gmail)
    set_history_id(new_history_id)

    if processed:
        logger.info("Ciclo completato: %d contatti processati.", processed)
    else:
        logger.debug("Nessuna nuova email da processare.")

    return seen


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    once = "--once" in sys.argv

    logger.info("=" * 62)
    logger.info("  Gmail → HubSpot Contact Sync")
    logger.info("  Modalità: %s", "singola esecuzione" if once else f"loop ogni {POLL_INTERVAL}s")
    logger.info("  Attività HubSpot: %s", "abilitata" if CREATE_ACTIVITY else "disabilitata")
    logger.info("=" * 62)

    try:
        gmail = gmail_client.get_service()
        hs = hubspot_client.get_client()
    except Exception as exc:
        logger.critical("Inizializzazione fallita: %s", exc)
        sys.exit(1)

    seen: Set[str] = set()

    while True:
        try:
            seen = run_cycle(gmail, hs, seen)
        except KeyboardInterrupt:
            logger.info("Interruzione manuale. Arresto.")
            sys.exit(0)
        except Exception as exc:
            logger.error("Errore nel ciclo di sincronizzazione: %s", exc, exc_info=True)

        if once:
            break

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
