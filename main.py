#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail in arrivo e sincronizza i mittenti come contatti HubSpot.

Uso:
    python main.py              # loop continuo (Ctrl+C per fermare)
    python main.py --once       # singolo ciclo e termina
"""

import argparse
import logging
import sys
import time

from gmail_hubspot_sync.config import load_config
from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.sync_service import SyncService


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un singolo ciclo di sync invece del loop continuo",
    )
    args = parser.parse_args()

    try:
        config = load_config()
    except (ValueError, KeyError) as e:
        print(f"Errore configurazione: {e}", file=sys.stderr)
        print("Copia .env.example in .env e compila i valori richiesti.", file=sys.stderr)
        sys.exit(1)

    _setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    gmail = GmailClient(
        credentials_file=config.gmail_credentials_file,
        token_file=config.gmail_token_file,
        scopes=config.gmail_scopes,
    )
    hubspot = HubSpotClient(access_token=config.hubspot_access_token)

    service = SyncService(
        gmail=gmail,
        hubspot=hubspot,
        processed_label_name=config.gmail_processed_label,
        enable_activity_note=config.enable_activity_note,
    )

    logger.info("=== Gmail → HubSpot Sync avviato ===")
    service.setup()

    if args.once:
        outcomes = service.run_once()
        _print_summary(outcomes)
        return

    # Loop continuo
    logger.info(f"Polling ogni {config.poll_interval}s | Ctrl+C per fermare")
    try:
        while True:
            outcomes = service.run_once()
            if outcomes:
                _print_summary(outcomes)
            time.sleep(config.poll_interval)
    except KeyboardInterrupt:
        logger.info("Sync interrotto dall'utente.")


def _print_summary(outcomes) -> None:
    if not outcomes:
        return

    from gmail_hubspot_sync.hubspot_client import SyncResult

    created = sum(1 for o in outcomes if o.result == SyncResult.CREATED)
    updated = sum(1 for o in outcomes if o.result == SyncResult.UPDATED)
    skipped = sum(1 for o in outcomes if o.result == SyncResult.SKIPPED)

    print(
        f"\n── Riepilogo ciclo ──────────────────────────────\n"
        f"  ✚ Creati:    {created}\n"
        f"  ↻ Aggiornati: {updated}\n"
        f"  – Ignorati:  {skipped}\n"
        f"  Totale:      {len(outcomes)}\n"
        f"────────────────────────────────────────────────\n"
    )


if __name__ == "__main__":
    main()
