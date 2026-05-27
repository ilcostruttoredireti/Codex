#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Polls Gmail for new inbox messages on a configurable interval, extracts sender
contact data, and creates or updates contacts in HubSpot CRM.

Usage:
    python main.py [--once]

    --once   Run a single sync cycle then exit (useful for cron / testing).
"""

import argparse
import logging
import signal
import sys
import time

from config import POLL_INTERVAL_SECONDS
from sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_running = True


def _handle_signal(signum, frame):
    global _running
    logger.info("Signal %s received — shutting down gracefully.", signum)
    _running = False


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one sync cycle and exit (for cron / testing)",
    )
    args = parser.parse_args()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    engine = SyncEngine()

    logger.info("Gmail→HubSpot sync started (poll interval: %ds)", POLL_INTERVAL_SECONDS)

    while _running:
        try:
            results = engine.run_once()
            if results:
                logger.info("─── Batch summary ───────────────────────────")
                for r in results:
                    logger.info("  %s", r)
                logger.info("─────────────────────────────────────────────")
                created = sum(1 for r in results if r.status.value == "Creato")
                updated = sum(1 for r in results if r.status.value == "Aggiornato")
                ignored = sum(1 for r in results if r.status.value == "Ignorato")
                errors  = sum(1 for r in results if r.status.value == "Errore")
                logger.info(
                    "Totale: %d  |  Creati: %d  Aggiornati: %d  Ignorati: %d  Errori: %d",
                    len(results), created, updated, ignored, errors,
                )
            else:
                logger.info("Nessuna nuova email rilevata.")
        except Exception:
            logger.exception("Errore imprevisto nel ciclo di sync")

        if args.once:
            break

        logger.info("Prossimo controllo tra %d secondi…", POLL_INTERVAL_SECONDS)
        # Sleep in small increments so SIGINT/SIGTERM are handled promptly
        deadline = time.monotonic() + POLL_INTERVAL_SECONDS
        while _running and time.monotonic() < deadline:
            time.sleep(1)

    logger.info("Sync terminato.")


if __name__ == "__main__":
    main()
