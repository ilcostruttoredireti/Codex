#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
==============================
Monitora le email in arrivo su Gmail e sincronizza automaticamente
i mittenti come contatti in HubSpot.

Utilizzo:
  python main.py              # modalità loop continuo
  python main.py --once       # singola iterazione (utile per cron)
  python main.py --dry-run    # simula senza scrivere su HubSpot
  python main.py --version    # mostra versione
"""

import argparse
import sys
import logging

from gmail_hubspot_sync import __version__
from gmail_hubspot_sync.config import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="gmail-hubspot-sync",
        description="Sincronizza i mittenti Gmail come contatti HubSpot.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui una singola iterazione invece del loop continuo.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Leggi le email ma non scrivere nulla su HubSpot.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log = setup_logging()

    log.info("Gmail → HubSpot Sync v%s", __version__)

    if args.dry_run:
        log.warning(
            "Modalità DRY-RUN attiva: nessuna scrittura su HubSpot."
        )
        # Patch HubSpotClient per non scrivere
        from gmail_hubspot_sync import hubspot_client
        from gmail_hubspot_sync.hubspot_client import SyncResult, SyncStatus

        def _noop_sync(self, email, **kwargs):
            log.info("[DRY-RUN] Avrei sincronizzato: %s", email)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                detail="dry-run",
            )

        hubspot_client.HubSpotClient.sync_sender = _noop_sync  # type: ignore[method-assign]

    try:
        from gmail_hubspot_sync.sync import GmailHubSpotSync
        sync = GmailHubSpotSync()

        if args.once:
            log.info("Modalità singola iterazione.")
            results = sync.run_once()
            if not results:
                log.info("Nessuna nuova email trovata.")
        else:
            sync.run_loop()

    except FileNotFoundError as exc:
        log.error("File mancante: %s", exc)
        return 1
    except ValueError as exc:
        log.error("Configurazione errata: %s", exc)
        return 1
    except KeyboardInterrupt:
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
