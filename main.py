#!/usr/bin/env python3
"""Gmail → HubSpot contact sync monitor.

Avvio:
    python main.py

Variabili d'ambiente richieste (.env):
    HUBSPOT_ACCESS_TOKEN   Token dell'app privata HubSpot
    GMAIL_CREDENTIALS_FILE Percorso delle credenziali OAuth2 di Gmail
"""

import os
import sys
import time
import signal
import logging
from dotenv import load_dotenv

load_dotenv()

from src.gmail_monitor import GmailMonitor
from src.contact_processor import ContactProcessor
from src.hubspot_sync import HubSpotSync

_LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

POLL_INTERVAL = int(os.environ.get('POLL_INTERVAL', '60'))
_running = True


def _handle_shutdown(sig, frame):
    global _running
    log.info("Segnale di arresto ricevuto, chiusura in corso...")
    _running = False


def _process(email_data, processor, hubspot):
    contact = processor.extract(email_data)
    if not contact:
        log.debug(f"Email ignorata (mittente non parsabile): {email_data.get('from', '?')}")
        return None
    return hubspot.sync_contact(contact)


def main():
    signal.signal(signal.SIGINT, _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    log.info("=== Gmail → HubSpot Sync Monitor ===")

    try:
        gmail = GmailMonitor()
        processor = ContactProcessor()
        hubspot = HubSpotSync()
    except Exception as e:
        log.critical(f"Inizializzazione fallita: {e}")
        sys.exit(1)

    log.info(f"Monitor attivo. Polling ogni {POLL_INTERVAL}s. Premi Ctrl+C per fermare.\n")

    while _running:
        emails = gmail.get_new_emails()

        if emails:
            log.info(f"Trovate {len(emails)} nuova/e email")

        for email in emails:
            result = _process(email, processor, hubspot)
            if result:
                print(
                    f"  Stato: {result['status']:<12} | "
                    f"Email: {result['email']:<40} | "
                    f"ID HubSpot: {result['id']}"
                )

        if _running:
            time.sleep(POLL_INTERVAL)

    log.info("Monitor fermato.")


if __name__ == '__main__':
    main()
