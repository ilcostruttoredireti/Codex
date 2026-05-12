#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors incoming Gmail messages and automatically creates or updates
HubSpot contacts from sender information.

Usage
-----
    # First run (opens browser for Gmail OAuth2):
    python main.py --auth

    # Single sync cycle:
    python main.py --once

    # Continuous sync (default):
    python main.py

Environment variables (copy .env.example → .env):
    HUBSPOT_ACCESS_TOKEN   HubSpot Private App token (required)
    GMAIL_CREDENTIALS_FILE Path to Google OAuth2 credentials JSON
    POLL_INTERVAL_SECONDS  Seconds between Gmail polls (default 60)
    MAX_EMAILS_PER_POLL    Max messages fetched per cycle (default 50)
"""

import argparse
import logging
import sys

from gmail_hubspot_sync import config
from gmail_hubspot_sync.sync import GmailHubSpotSync


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    _configure_logging()

    parser = argparse.ArgumentParser(
        description="Sincronizza i contatti Gmail → HubSpot"
    )
    parser.add_argument(
        "--auth",
        action="store_true",
        help="Esegui solo il flusso di autenticazione Gmail e poi esci",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un solo ciclo di sincronizzazione e poi esci",
    )
    args = parser.parse_args()

    syncer = GmailHubSpotSync()

    print("Gmail → HubSpot Contact Sync")
    print("=" * 40)

    syncer.authenticate()

    if args.auth:
        print("Autenticazione completata. Token salvato.")
        sys.exit(0)

    if args.once:
        results = syncer.run_once()
        _print_summary(results)
        sys.exit(0)

    # Default: continuous loop
    try:
        syncer.run_forever()
    except KeyboardInterrupt:
        print("\nSync interrotto dall'utente.")
        sys.exit(0)


def _print_summary(results: list) -> None:
    from gmail_hubspot_sync.hubspot_client import SyncStatus

    print("\n--- Riepilogo ---")
    if not results:
        print("Nessuna email processata.")
        return

    for r in results:
        print(f"  {r}")

    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
    print(f"\nTotale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {ignored}")


if __name__ == "__main__":
    main()
