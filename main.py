"""
Gmail → HubSpot Contact Sync
-----------------------------
Monitora la posta in arrivo di Gmail e sincronizza i mittenti in HubSpot.

Avvio rapido:
    pip install -r requirements.txt
    cp .env.example .env          # e compila HUBSPOT_API_KEY
    python main.py                # segui il prompt OAuth al primo avvio

Variabili d'ambiente richieste (vedi .env.example):
    HUBSPOT_API_KEY   – Private App token HubSpot (CRM scopes)
    POLL_INTERVAL     – Secondi tra un ciclo e l'altro (default: 60)
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _check_env():
    missing = [v for v in ("HUBSPOT_API_KEY",) if not os.environ.get(v)]
    if missing:
        logger.error("Variabili d'ambiente mancanti: %s", ", ".join(missing))
        sys.exit(1)


def main():
    _check_env()

    from gmail_client import GmailClient
    from hubspot_client import HubSpotClient
    from sync import sync_sender_to_hubspot

    poll_interval = int(os.environ.get("POLL_INTERVAL", 60))

    logger.info("=== Gmail → HubSpot Sync avviato (intervallo: %ds) ===", poll_interval)

    gmail = GmailClient(
        credentials_file=os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        token_file=os.environ.get("GMAIL_TOKEN_FILE", "token.json"),
    )
    hubspot = HubSpotClient()

    while True:
        try:
            messages = gmail.get_new_messages()
            if messages:
                logger.info("Trovate %d nuove email.", len(messages))
                for msg in messages:
                    result = sync_sender_to_hubspot(msg, hubspot)
                    logger.info(str(result))
            else:
                logger.debug("Nessuna nuova email.")
        except KeyboardInterrupt:
            logger.info("Interruzione manuale. Uscita.")
            break
        except Exception as exc:
            logger.error("Errore nel ciclo principale: %s", exc, exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
