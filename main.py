#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox at a configurable interval and upserts sender contacts
into HubSpot CRM.

Usage:
    python main.py                  # run once then exit
    python main.py --loop           # run continuously (interval from .env)
    python main.py --loop --interval 300   # override interval (seconds)
"""

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.state_manager import StateManager
from gmail_hubspot_sync.sync_engine import SyncEngine, SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def build_engine() -> SyncEngine:
    hubspot_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        logger.error("HUBSPOT_ACCESS_TOKEN non impostato nel file .env")
        sys.exit(1)

    credentials_path = os.environ.get("GMAIL_CREDENTIALS_PATH", "credentials.json")
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "token.json")
    state_path = os.environ.get("STATE_FILE_PATH", "sync_state.json")
    batch_size = int(os.environ.get("BATCH_SIZE", "50"))

    gmail = GmailClient(credentials_path=credentials_path, token_path=token_path)
    hubspot = HubSpotClient(access_token=hubspot_token)
    state = StateManager(path=state_path)

    return SyncEngine(gmail=gmail, hubspot=hubspot, state=state, batch_size=batch_size)


def run_cycle(engine: SyncEngine) -> None:
    results = engine.run_once()

    created = [r for r in results if r.status == SyncStatus.CREATED]
    updated = [r for r in results if r.status == SyncStatus.UPDATED]
    ignored = [r for r in results if r.status == SyncStatus.IGNORED]

    logger.info(
        "Ciclo completato — Creati: %d  Aggiornati: %d  Ignorati: %d",
        len(created),
        len(updated),
        len(ignored),
    )

    for r in results:
        logger.info(str(r))


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot sync")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Esegui in loop continuo",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_SECONDS", "300")),
        help="Intervallo di polling in secondi (default: 300)",
    )
    args = parser.parse_args()

    engine = build_engine()

    if args.loop:
        logger.info("Avvio monitoraggio continuo (intervallo: %ds)", args.interval)
        while True:
            try:
                run_cycle(engine)
            except Exception as exc:
                logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)
            logger.info("Prossima esecuzione tra %d secondi...", args.interval)
            time.sleep(args.interval)
    else:
        run_cycle(engine)


if __name__ == "__main__":
    main()
