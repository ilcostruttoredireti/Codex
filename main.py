"""
Gmail → HubSpot Contact Sync
=============================
Continuously polls Gmail inbox for new emails and upserts sender
contacts into HubSpot CRM.

Usage:
    python main.py            # continuous loop (default 5-minute interval)
    python main.py --once     # single pass then exit
    python main.py --interval 60   # override poll interval (seconds)
"""

import argparse
import logging
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from contact_extractor import extract_contact
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import (
    get_last_check_timestamp,
    init_db,
    is_processed,
    mark_processed,
    set_last_check_timestamp,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Status icons for the console summary line
_ICON = {
    "created": "✓ Creato   ",
    "updated": "↑ Aggiornato",
    "skipped": "→ Ignorato  ",
    "error":   "✗ Errore    ",
    "ignored": "– Ignorato  ",
}


def run_cycle(gmail: GmailClient, hubspot: HubSpotClient) -> tuple[int, int]:
    """
    Fetch new inbox messages, upsert HubSpot contacts, return
    (processed_count, error_count).
    """
    last_ts = get_last_check_timestamp()
    now_ts = int(time.time())

    processed = 0
    errors = 0

    for message in gmail.get_messages_after(since_epoch=last_ts):
        msg_id = message["id"]

        if is_processed(msg_id):
            continue

        from_header = gmail.get_header(message, "From")
        subject = gmail.get_header(message, "Subject")

        if not from_header:
            mark_processed(msg_id, "", None, "ignored")
            continue

        contact = extract_contact(from_header)

        if contact is None:
            mark_processed(msg_id, from_header, None, "ignored")
            log.debug("Ignored (filtered): %s", from_header)
            continue

        # --- HubSpot upsert ---
        existing = hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id, status = hubspot.update_contact(
                str(existing.id), contact, existing
            )
        else:
            contact_id, status = hubspot.create_contact(contact)

        if contact_id and status in ("created", "updated"):
            hubspot.add_email_activity(contact_id, contact.email, subject)

        mark_processed(msg_id, contact.email, contact_id, status)

        icon = _ICON.get(status, status)
        log.info("%s | %-40s | HubSpot ID: %s", icon, contact.email, contact_id or "—")

        processed += 1
        if status == "error":
            errors += 1

    set_last_check_timestamp(now_ts)
    return processed, errors


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Polling interval in seconds (default: 300)",
    )
    args = parser.parse_args()

    log.info("=== Gmail → HubSpot Sync avviato ===")
    init_db()

    try:
        gmail = GmailClient()
        hubspot = HubSpotClient()
    except Exception as exc:
        log.error("Inizializzazione fallita: %s", exc)
        sys.exit(1)

    if args.once:
        processed, errors = run_cycle(gmail, hubspot)
        log.info("Completato: %d email processate, %d errori.", processed, errors)
        return

    log.info("Polling ogni %d secondi. Premi Ctrl+C per fermare.", args.interval)

    while True:
        try:
            log.info("Controllo nuove email in arrivo…")
            processed, errors = run_cycle(gmail, hubspot)
            log.info(
                "Ciclo completato: %d email processate, %d errori.", processed, errors
            )
        except KeyboardInterrupt:
            log.info("Interruzione manuale. Uscita.")
            break
        except Exception as exc:  # noqa: BLE001
            log.error("Errore nel ciclo di polling: %s", exc)

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            log.info("Interruzione manuale. Uscita.")
            break


if __name__ == "__main__":
    main()
