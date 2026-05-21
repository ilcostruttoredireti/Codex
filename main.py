#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail e sincronizza automaticamente i mittenti su HubSpot.

Uso:
    python main.py                  # avvia il loop continuo
    python main.py --once           # esegue un solo ciclo e termina
    python main.py --dry-run        # mostra cosa farebbe senza scrivere su HubSpot
"""

import argparse
import logging
import time
import os
import sys

import hubspot
from dotenv import load_dotenv

from gmail_auth import get_gmail_service
from gmail_reader import fetch_new_senders
from hubspot_sync import sync_contact, SyncStatus

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _print_result_table(results: list) -> None:
    if not results:
        log.info("Nessuna nuova email da processare.")
        return

    header = f"{'STATO':<12} {'EMAIL':<40} {'ID HUBSPOT':<15} {'NOTE'}"
    log.info("-" * len(header))
    log.info(header)
    log.info("-" * len(header))
    for r in results:
        log.info(
            f"{r.status.value:<12} {r.email:<40} {str(r.contact_id or '-'):<15} {r.reason}"
        )
    log.info("-" * len(header))

    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
    log.info(f"Riepilogo: {created} creati | {updated} aggiornati | {ignored} ignorati")


def run_cycle(gmail_service, hubspot_client, owner_email: str,
              gmail_label: str, dry_run: bool) -> None:
    log.info("Recupero nuove email...")
    senders = fetch_new_senders(gmail_service, owner_email, gmail_label)

    if not senders:
        log.info("Nessun nuovo mittente trovato.")
        return

    log.info(f"Trovati {len(senders)} nuovi mittenti.")
    results = []

    for info in senders:
        log.debug(f"Processo: {info.email} | Oggetto: {info.subject}")

        if dry_run:
            from hubspot_sync import SyncResult
            results.append(SyncResult(
                status=SyncStatus.IGNORED,
                email=info.email,
                contact_id=None,
                reason="[dry-run]",
            ))
            continue

        result = sync_contact(hubspot_client, info)
        results.append(result)

    _print_result_table(results)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Esegui un ciclo singolo")
    parser.add_argument("--dry-run", action="store_true",
                        help="Mostra le azioni senza scrivere su HubSpot")
    args = parser.parse_args()

    # Validazione configurazione
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        log.error("HUBSPOT_ACCESS_TOKEN non configurato nel file .env")
        sys.exit(1)

    owner_email = os.getenv("GMAIL_OWNER_EMAIL", "")
    gmail_label = os.getenv("GMAIL_LABEL", "INBOX")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")

    log.info("Autenticazione Gmail...")
    gmail_service = get_gmail_service(credentials_file, token_file)
    log.info("Gmail autenticato.")

    hubspot_client = hubspot.HubSpot(access_token=hubspot_token)
    log.info("HubSpot client inizializzato.")

    if args.dry_run:
        log.info("Modalità DRY-RUN attiva — nessuna modifica su HubSpot.")

    if args.once:
        run_cycle(gmail_service, hubspot_client, owner_email, gmail_label, args.dry_run)
        return

    log.info(f"Avvio loop continuo (intervallo: {poll_interval}s). Premi Ctrl+C per fermare.")
    try:
        while True:
            run_cycle(gmail_service, hubspot_client, owner_email, gmail_label, args.dry_run)
            log.info(f"Attendo {poll_interval} secondi...")
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log.info("Interruzione richiesta. Chiusura.")


if __name__ == "__main__":
    main()
