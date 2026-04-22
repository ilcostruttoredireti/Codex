#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors incoming Gmail messages and automatically creates or updates
HubSpot contacts from each sender, avoiding duplicates.

Setup:
  1. Copy .env.example to .env and fill in HUBSPOT_TOKEN
  2. Download credentials.json from Google Cloud Console (Gmail API, Desktop app)
  3. pip install -r requirements.txt
  4. python main.py          (first run opens a browser for Gmail OAuth)

Output per ogni email processata:
  ✅ CREATO     | Email: user@company.com          | HubSpot ID: 12345
  🔄 AGGIORNATO | Email: user@company.com          | HubSpot ID: 12345
  ⏭️ IGNORATO  | Email: noreply@service.com       | HubSpot ID: N/A (indirizzo sistema)
"""

import logging
import sys
import time

from gmail_hubspot_sync import config
from gmail_hubspot_sync.gmail import fetch_message_sender, get_gmail_service, get_new_inbox_message_ids
from gmail_hubspot_sync.state import SyncState
from gmail_hubspot_sync.sync import sync_sender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _validate_config() -> None:
    if not config.HUBSPOT_TOKEN:
        logger.error(
            "HUBSPOT_TOKEN mancante. "
            "Imposta la variabile d'ambiente o crea un file .env (vedi .env.example)."
        )
        sys.exit(1)


def process_message(service, message_id: str, state: SyncState) -> None:
    """Fetch sender info, sync to HubSpot, and mark as processed."""
    if state.is_processed(message_id):
        return

    sender = fetch_message_sender(service, message_id)
    state.mark_processed(message_id)  # mark before sync to avoid infinite retries on parse errors

    if sender is None:
        return

    try:
        result = sync_sender(sender)
        logger.info(str(result))
    except Exception as exc:
        logger.error("Errore nella sync del mittente %s: %s", sender.get("email"), exc)


def run_poll_loop(service, state: SyncState) -> None:
    logger.info(
        "Monitoraggio Gmail attivo — polling ogni %ds. Premi Ctrl+C per fermare.",
        config.POLL_INTERVAL_SECONDS,
    )

    while True:
        try:
            new_ids = get_new_inbox_message_ids(service, state)
            if new_ids:
                logger.info("📬 %d nuov%s messaggio/i da processare", len(new_ids), "o" if len(new_ids) == 1 else "i")
            for msg_id in new_ids:
                process_message(service, msg_id, state)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error("Errore nel ciclo di polling: %s", exc)

        time.sleep(config.POLL_INTERVAL_SECONDS)


def main() -> None:
    _validate_config()

    logger.info("=" * 60)
    logger.info("  Gmail → HubSpot Contact Sync")
    logger.info("=" * 60)

    state = SyncState(config.STATE_FILE)
    service = get_gmail_service()

    try:
        run_poll_loop(service, state)
    except KeyboardInterrupt:
        logger.info("Sync interrotta dall'utente.")


if __name__ == "__main__":
    main()
