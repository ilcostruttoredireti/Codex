#!/usr/bin/env python3
import argparse
import logging
import sys

import config
from sync import GmailHubSpotSync


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _validate_config() -> list[str]:
    import os

    errors: list[str] = []
    if not config.HUBSPOT_ACCESS_TOKEN:
        errors.append("HUBSPOT_ACCESS_TOKEN non impostato nel file .env")
    if not os.path.exists(config.GMAIL_CREDENTIALS_FILE):
        errors.append(
            f"File credenziali Gmail non trovato: {config.GMAIL_CREDENTIALS_FILE}"
        )
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="gmail-hubspot-sync",
        description="Monitora Gmail e sincronizza i mittenti come contatti in HubSpot.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui una singola scansione invece del monitoraggio continuo.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.POLL_INTERVAL_SECONDS,
        metavar="SECONDI",
        help=f"Intervallo di polling in secondi (default: {config.POLL_INTERVAL_SECONDS}).",
    )
    parser.add_argument(
        "--log-level",
        default=config.LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Livello di log (default: INFO).",
    )
    args = parser.parse_args()

    _setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    errors = _validate_config()
    if errors:
        for err in errors:
            logger.error(err)
        sys.exit(1)

    syncer = GmailHubSpotSync()
    syncer.initialize()

    if args.once:
        results = syncer.run_once()
        created = sum(1 for r in results if r.status.value == "Creato")
        updated = sum(1 for r in results if r.status.value == "Aggiornato")
        ignored = sum(1 for r in results if r.status.value == "Ignorato")
        errors_count = sum(1 for r in results if r.status.value == "Errore")
        logger.info(
            f"Completato — Creati: {created}, Aggiornati: {updated}, "
            f"Ignorati: {ignored}, Errori: {errors_count}"
        )
    else:
        syncer.run_continuous(interval=args.interval)


if __name__ == "__main__":
    main()
