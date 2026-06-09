#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Continuously monitors the Gmail inbox and upserts senders as HubSpot contacts.
"""
import logging
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from gmail_hubspot_sync.config import Config
from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.models import SyncStatus
from gmail_hubspot_sync.sync_engine import SyncEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

_running = True


def _handle_signal(sig, frame):
    global _running
    logger.info("Received signal %s — stopping after current cycle", sig)
    _running = False


def _print_results(results: list) -> None:
    if not results:
        return
    created = [r for r in results if r.status == SyncStatus.CREATED]
    updated = [r for r in results if r.status == SyncStatus.UPDATED]
    ignored = [r for r in results if r.status == SyncStatus.IGNORED]

    print(f"\n{'─' * 60}")
    print(f"  Ciclo completato — {len(results)} email elaborate")
    print(f"  Creati: {len(created)}  |  Aggiornati: {len(updated)}  |  Ignorati: {len(ignored)}")
    print(f"{'─' * 60}")
    for r in results:
        print(f"  {r}")
    print()


def main() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        config = Config.from_env()
        config.validate()
    except (ValueError, FileNotFoundError) as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
    try:
        gmail.authenticate()
        gmail.ensure_processed_label()
    except Exception as exc:
        logger.error("Gmail auth failed: %s", exc)
        sys.exit(1)

    hubspot = HubSpotClient(config.hubspot_access_token, config.contact_source)
    engine = SyncEngine(gmail, hubspot, config.max_emails_per_cycle)

    logger.info(
        "Sync avviato — intervallo=%ds max_per_ciclo=%d",
        config.poll_interval_seconds,
        config.max_emails_per_cycle,
    )

    while _running:
        try:
            results = engine.run_cycle()
            _print_results(results)
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        if _running:
            logger.debug("Prossimo ciclo tra %d secondi", config.poll_interval_seconds)
            time.sleep(config.poll_interval_seconds)

    logger.info("Sync terminato")


if __name__ == "__main__":
    main()
