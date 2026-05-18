"""
Gmail → HubSpot Contact Sync
Monitora la inbox Gmail e sincronizza i mittenti come contatti HubSpot.
"""

import logging
import signal
import sys
import time

import config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from processor import ContactProcessor
from state_manager import StateManager

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _handle_shutdown(sig, frame):
    logger.info("Interruzione ricevuta — arresto in corso.")
    sys.exit(0)


def main():
    logger.info("=" * 55)
    logger.info("  Gmail → HubSpot Contact Sync")
    logger.info("=" * 55)

    if not config.HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY non configurato. Controlla il file .env")
        sys.exit(1)

    logger.info("Autenticazione Gmail in corso...")
    gmail = GmailClient(
        credentials_file=config.GMAIL_CREDENTIALS_FILE,
        token_file=config.GMAIL_TOKEN_FILE,
    )
    logger.info("Connesso come: %s", gmail.user_email)

    hubspot = HubSpotClient(api_key=config.HUBSPOT_API_KEY)
    state = StateManager(db_path=config.STATE_DB_PATH)
    processor = ContactProcessor(
        gmail_client=gmail,
        hubspot_client=hubspot,
        state_manager=state,
        max_per_run=config.MAX_EMAILS_PER_RUN,
    )

    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    logger.info(
        "Monitoraggio attivo — polling ogni %ds | Output: CREATO / AGGIORNATO / IGNORATO",
        config.POLL_INTERVAL,
    )
    logger.info("-" * 55)

    while True:
        try:
            processor.process_new_emails()
        except Exception as exc:
            logger.error("Errore imprevisto: %s", exc, exc_info=True)

        time.sleep(config.POLL_INTERVAL)


if __name__ == "__main__":
    main()
