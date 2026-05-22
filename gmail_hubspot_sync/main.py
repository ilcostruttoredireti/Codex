#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors Gmail inbox and syncs new sender contacts to HubSpot.

Usage:
    python main.py            # run once (cron-friendly)
    python main.py --loop     # continuous polling loop
    python main.py --dry-run  # print what would happen, no writes
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from config import Config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import StateManager
from sync_engine import SyncEngine, SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gmail_hubspot_sync")


def build_engine(config: Config) -> SyncEngine:
    gmail = GmailClient(config)
    hubspot = HubSpotClient(config)
    state = StateManager(config.STATE_FILE)
    return SyncEngine(config, gmail, hubspot, state)


def print_results(results) -> None:
    if not results:
        logger.info("Nessun nuovo messaggio da processare.")
        return

    created = [r for r in results if r.status == SyncStatus.CREATED]
    updated = [r for r in results if r.status == SyncStatus.UPDATED]
    ignored = [r for r in results if r.status == SyncStatus.IGNORED]

    print(f"\n{'─'*60}")
    print(f"  Elaborati: {len(results)}  |  Creati: {len(created)}  |  Aggiornati: {len(updated)}  |  Ignorati: {len(ignored)}")
    print(f"{'─'*60}")

    for r in results:
        icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️"}.get(r.status, "•")
        hs_id = f"ID: {r.hubspot_id}" if r.hubspot_id else ""
        reason = f"({r.reason})" if r.reason else ""
        print(f"  {icon} [{r.status}]  {r.email}  {hs_id}  {reason}".rstrip())

    print(f"{'─'*60}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--loop", action="store_true", help="Run in polling loop")
    parser.add_argument("--dry-run", action="store_true", help="No writes — log only")
    parser.add_argument("--interval", type=int, default=None, help="Override poll interval (seconds)")
    args = parser.parse_args()

    config = Config()

    if args.interval:
        config.POLL_INTERVAL_SECONDS = args.interval

    if not config.HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY non impostata. Controlla il file .env")
        sys.exit(1)

    engine = build_engine(config)

    if args.loop:
        logger.info("Avvio loop di monitoraggio (intervallo: %ds) …", config.POLL_INTERVAL_SECONDS)
        while True:
            try:
                results = engine.run_once()
                print_results(results)
            except KeyboardInterrupt:
                logger.info("Interruzione manuale.")
                break
            except Exception as exc:
                logger.error("Errore durante la sincronizzazione: %s", exc)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    else:
        results = engine.run_once()
        print_results(results)


if __name__ == "__main__":
    main()
