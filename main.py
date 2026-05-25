#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
==============================
Monitora le email in arrivo su Gmail ed sincronizza automaticamente
i mittenti come contatti in HubSpot CRM.

Uso:
    python main.py                  # loop continuo
    RUN_ONCE=true python main.py    # esecuzione singola ed uscita

Configurazione (file .env o variabili d'ambiente):
    HUBSPOT_ACCESS_TOKEN    obbligatorio
    GMAIL_CREDENTIALS_FILE  default: credentials.json
    GMAIL_POLL_INTERVAL     default: 60 (secondi)
    GMAIL_LABEL             default: INBOX
    LOG_LEVEL               default: INFO
    RUN_ONCE                default: false
"""

import logging
import sys

from gmail_hubspot_sync.config import AppConfig
from gmail_hubspot_sync.sync_engine import SyncEngine


def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    config = AppConfig()
    setup_logging(config.log_level)

    logger = logging.getLogger(__name__)
    logger.info("Avvio Gmail → HubSpot Sync")

    try:
        config.validate()
    except ValueError as exc:
        logger.error("❌ Errore di configurazione:\n%s", exc)
        sys.exit(1)

    engine = SyncEngine(config)
    engine.run()


if __name__ == "__main__":
    main()
