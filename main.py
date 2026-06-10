#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Polls Gmail for new inbox messages and syncs sender contacts to HubSpot.

Usage:
    python main.py

Required environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN   – HubSpot Private App token
    GMAIL_CREDENTIALS_PATH – path to Google OAuth credentials.json
"""

import logging
import signal
import sys
import time

from config import load_config
from sync import GmailHubSpotSyncer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_BANNER = """\
╔══════════════════════════════════════════════════════════════════════╗
║           Gmail → HubSpot  |  Sync contatti in arrivo               ║
╚══════════════════════════════════════════════════════════════════════╝
  {:<10}  {:<42}  {}
{}""".format(
    "STATO", "EMAIL CONTATTO", "ID HUBSPOT", "─" * 72
)


def main() -> None:
    config = load_config()
    syncer = GmailHubSpotSyncer(config)

    print(_BANNER)
    logger.info(
        "Avviato — polling ogni %ds  |  state: %s",
        config.poll_interval_seconds,
        config.state_file,
    )

    def _shutdown(sig, _frame):
        logger.info("Arresto in corso...")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        try:
            syncer.run_once()
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)
        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    main()
