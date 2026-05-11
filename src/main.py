"""
Gmail → HubSpot contact sync

Monitors Gmail INBOX continuously, extracts sender data from each new message,
and creates or updates the corresponding HubSpot contact.

Output per email processed:
    Stato:          Creato | Aggiornato | Ignorato
    Email contatto: sender@example.com
    ID HubSpot:     12345678
"""

import logging
import sys
import time
from datetime import datetime

from config import HUBSPOT_ACCESS_TOKEN, POLL_INTERVAL_SECONDS
from contact_extractor import extract_contact
from gmail_monitor import GmailMonitor
from hubspot_sync import HubSpotSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("gmail_hubspot_sync.log"),
    ],
)
logger = logging.getLogger(__name__)

_SEP = "=" * 52


def _print_result(status: str, email: str, contact_id: str | None, contact) -> None:
    name = " ".join(filter(None, [contact.first_name, contact.last_name]))
    print(
        f"\n{_SEP}\n"
        f"Stato:          {status}\n"
        f"Email contatto: {email}\n"
        f"ID HubSpot:     {contact_id or 'N/A'}\n"
        f"Nome:           {name or 'N/A'}\n"
        f"Azienda:        {contact.company or 'N/A'}\n"
        f"{_SEP}"
    )


def process_once(monitor: GmailMonitor, syncer: HubSpotSync) -> int:
    processed = 0
    for message in monitor.new_inbox_messages():
        contact = extract_contact(message)
        if contact is None:
            logger.debug("Skipped message %s – no parseable sender", message.get("id"))
            continue

        try:
            status, contact_id = syncer.sync_contact(contact)
            _print_result(status, contact.email, contact_id, contact)
            processed += 1
        except Exception as exc:
            logger.error("Unexpected error syncing %s: %s", contact.email, exc, exc_info=True)

    return processed


def main() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN is not set. Configure it in .env and retry.")
        sys.exit(1)

    logger.info("Initialising Gmail monitor…")
    monitor = GmailMonitor()

    logger.info("Initialising HubSpot sync…")
    syncer = HubSpotSync()

    logger.info("Monitoring loop started — poll interval: %ds", POLL_INTERVAL_SECONDS)

    while True:
        ts = datetime.now().strftime("%H:%M:%S")
        logger.info("[%s] Checking for new emails…", ts)

        try:
            n = process_once(monitor, syncer)
            logger.info("Processed %d new email(s).", n)
        except KeyboardInterrupt:
            logger.info("Interrupted by user — shutting down.")
            break
        except Exception as exc:
            logger.error("Loop error: %s", exc, exc_info=True)

        try:
            time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Interrupted by user — shutting down.")
            break


if __name__ == "__main__":
    main()
