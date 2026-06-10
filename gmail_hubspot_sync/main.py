#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitora la casella Gmail in arrivo ed esporta automaticamente i mittenti
come contatti HubSpot (crea nuovi / aggiorna esistenti / evita duplicati).

Uso:
    python main.py                  # modalità continua (loop)
    python main.py --once           # esegui un solo ciclo ed esci
    python main.py --full-scan      # scansione completa della inbox (primo avvio)
"""

import argparse
import logging
import sys
import time

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_PROCESSED_LABEL,
    GMAIL_SCOPES,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
    POLL_INTERVAL_SECONDS,
    STATE_FILE,
)
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state import SyncState
from sync import GmailHubSpotSyncer, SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")


def _validate_config() -> None:
    import os

    errors = []
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        errors.append(
            f"Gmail credentials non trovate: {GMAIL_CREDENTIALS_FILE}\n"
            "  → Scarica 'credentials.json' dalla Google Cloud Console e "
            "posizionalo nella stessa directory."
        )
    if not HUBSPOT_ACCESS_TOKEN:
        errors.append(
            "HUBSPOT_ACCESS_TOKEN non impostato.\n"
            "  → Crea un Private App in HubSpot e imposta la variabile d'ambiente."
        )
    if errors:
        for e in errors:
            logger.error(e)
        sys.exit(1)


def _build_syncer(full_scan: bool) -> GmailHubSpotSyncer:
    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_SCOPES)
    hs = HubSpotClient(HUBSPOT_ACCESS_TOKEN)
    state = SyncState(STATE_FILE)

    label_id = gmail.get_or_create_label(GMAIL_PROCESSED_LABEL)

    if full_scan:
        # Reset history so initial_scan() is triggered
        state.history_id = None  # type: ignore[assignment]

    return GmailHubSpotSyncer(
        gmail=gmail,
        hubspot=hs,
        state=state,
        processed_label_id=label_id,
        log_activity=True,
    )


def _print_summary(results: list) -> None:
    if not results:
        logger.info("Nessuna nuova email da processare.")
        return

    created = [r for r in results if r.status == SyncStatus.CREATED]
    updated = [r for r in results if r.status == SyncStatus.UPDATED]
    ignored = [r for r in results if r.status == SyncStatus.IGNORED]

    logger.info(
        "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
        len(created), len(updated), len(ignored),
    )
    for r in results:
        print(str(r))


def _run_once(syncer: GmailHubSpotSyncer) -> None:
    results = syncer.incremental_sync()
    _print_summary(results)


def _run_loop(syncer: GmailHubSpotSyncer) -> None:
    logger.info(
        "Avvio monitoraggio continuo — polling ogni %d secondi. Ctrl+C per uscire.",
        POLL_INTERVAL_SECONDS,
    )
    while True:
        try:
            _run_once(syncer)
        except Exception as exc:
            logger.error("Errore durante il ciclo di sync: %s", exc, exc_info=True)
        time.sleep(POLL_INTERVAL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un solo ciclo di sync ed esci",
    )
    parser.add_argument(
        "--full-scan",
        action="store_true",
        help="Forza la scansione completa della inbox (ignora lo stato precedente)",
    )
    args = parser.parse_args()

    _validate_config()
    syncer = _build_syncer(full_scan=args.full_scan)

    if args.once or args.full_scan:
        _run_once(syncer)
    else:
        _run_loop(syncer)


if __name__ == "__main__":
    main()
