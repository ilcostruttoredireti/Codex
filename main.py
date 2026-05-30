#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Usage:
  python main.py           # run continuously (polls every POLL_INTERVAL_SECONDS)
  python main.py --once    # single pass then exit
"""

import argparse
import logging
import time

from gmail_hubspot_sync.config import POLL_INTERVAL_SECONDS, STATE_FILE_PATH
from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.state_manager import StateManager
from gmail_hubspot_sync.sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def build_engine() -> SyncEngine:
    gmail = GmailClient()
    hubspot = HubSpotClient()
    state = StateManager(STATE_FILE_PATH)
    return SyncEngine(gmail, hubspot, state)


def print_summary(results) -> None:
    if not results:
        logger.info("Nessun nuovo messaggio da processare.")
        return
    print("\n" + "─" * 60)
    print(f"{'STATO':<12}  {'EMAIL CONTATTO':<35}  {'HUBSPOT ID'}")
    print("─" * 60)
    for r in results:
        print(f"{r.status.value:<12}  {r.email:<35}  {r.hubspot_id}")
    print("─" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Single pass then exit")
    args = parser.parse_args()

    engine = build_engine()

    if args.once:
        results = engine.run_once()
        print_summary(results)
        return

    logger.info("Avvio monitoraggio Gmail (intervallo: %ds) …", POLL_INTERVAL_SECONDS)
    while True:
        try:
            results = engine.run_once()
            print_summary(results)
        except Exception as exc:
            logger.error("Errore durante la sincronizzazione: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
