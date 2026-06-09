#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Usage:
    python main.py           # continuous polling loop
    python main.py --once    # single run then exit
"""
import argparse
import logging
import sys
import time

from config import LOG_LEVEL, POLLING_INTERVAL_SECONDS
from sync_engine import GmailHubSpotSync, Status

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format='%(asctime)s [%(levelname)s] %(message)s',
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

_ICONS = {
    Status.CREATED: '✅',
    Status.UPDATED: '🔄',
    Status.IGNORED: '⏭️',
    Status.ERROR:   '❌',
}

_LABELS = {
    Status.CREATED: 'CREATO',
    Status.UPDATED: 'AGGIORNATO',
    Status.IGNORED: 'IGNORATO',
    Status.ERROR:   'ERRORE',
}


def _print_results(results) -> None:
    if not results:
        logger.info("Nessuna nuova email da processare.")
        return
    logger.info(f"{'─' * 60}")
    logger.info(f"  {len(results)} email processata/e:")
    logger.info(f"{'─' * 60}")
    for r in results:
        icon = _ICONS.get(r.status, '?')
        label = _LABELS.get(r.status, r.status)
        hub_id = r.hubspot_id or 'N/A'
        logger.info(f"  {icon} {label:<12} | {r.email:<40} | ID: {hub_id}")
        if r.reason:
            logger.info(f"     Motivo: {r.reason}")
    logger.info(f"{'─' * 60}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument('--once', action='store_true', help="Run once then exit")
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("  Gmail → HubSpot Contact Sync avviato")
    logger.info("=" * 60)

    try:
        sync = GmailHubSpotSync()
    except ValueError as exc:
        logger.error(f"Configurazione non valida: {exc}")
        sys.exit(1)

    while True:
        try:
            results = sync.run()
            _print_results(results)
        except KeyboardInterrupt:
            logger.info("\nSync interrotto dall'utente.")
            break
        except Exception as exc:
            logger.error(f"Errore durante la sincronizzazione: {exc}", exc_info=True)

        if args.once:
            break

        logger.info(f"Prossimo controllo tra {POLLING_INTERVAL_SECONDS}s...")
        try:
            time.sleep(POLLING_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("\nSync interrotto dall'utente.")
            break


if __name__ == '__main__':
    main()
