#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync

Monitors all incoming Gmail messages and upserts sender contacts in HubSpot.

Usage:
    python main.py [--once] [--log-activity] [--interval SECONDS]

Options:
    --once          Process current inbox once, then exit (no polling loop)
    --log-activity  Create a HubSpot note/activity for each email processed
    --interval N    Override POLL_INTERVAL_SECONDS from .env (default: 60)
"""

import argparse
import logging
import time
import sys

from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.contact_sync import ContactSyncService, SyncStatus
from gmail_hubspot_sync.config import POLL_INTERVAL_SECONDS, HUBSPOT_ACCESS_TOKEN

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

SUMMARY_HEADER = (
    "\n{'─' * 60}\n"
    "  Stato        | Email                     | HubSpot ID\n"
    "{'─' * 60}"
)


def _print_result_table(results):
    print("\n" + "─" * 70)
    print(f"  {'Stato':<12} | {'Email':<35} | {'HubSpot ID'}")
    print("─" * 70)
    for r in results:
        status_it = r.status.value
        print(f"  {status_it:<12} | {r.email:<35} | {r.contact_id or '—'}")
    print("─" * 70)
    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    skipped = sum(1 for r in results if r.status == SyncStatus.SKIPPED)
    print(f"  Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {skipped}\n")


def run(gmail: GmailClient, sync: ContactSyncService, log_activity: bool,
        history_id: str) -> str:
    """Fetch new messages, sync contacts, return updated history_id."""
    senders, new_history_id = gmail.fetch_new_messages(since_history_id=history_id or None)

    if not senders:
        logger.info("Nessun nuovo messaggio.")
        return new_history_id or history_id

    logger.info("Trovati %d nuovi mittenti da processare.", len(senders))
    results = [sync.process(sender, log_activity=log_activity) for sender in senders]
    _print_result_table(results)

    return new_history_id or history_id


def main():
    if not HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN non impostato. Controlla il file .env.")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true",
                        help="Esegui una volta sola e termina")
    parser.add_argument("--log-activity", action="store_true",
                        help="Crea note HubSpot per ogni email processata")
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_SECONDS,
                        help=f"Secondi tra un polling e l'altro (default: {POLL_INTERVAL_SECONDS})")
    args = parser.parse_args()

    logger.info("Avvio Gmail → HubSpot sync (intervallo: %ds, log-activity: %s)",
                args.interval, args.log_activity)

    gmail = GmailClient()
    sync = ContactSyncService()
    history_id = ""

    if args.once:
        run(gmail, sync, args.log_activity, history_id)
        return

    logger.info("Modalità polling attiva. Premi Ctrl+C per uscire.")
    while True:
        try:
            history_id = run(gmail, sync, args.log_activity, history_id)
        except Exception as exc:
            logger.exception("Errore durante il ciclo di sync: %s", exc)

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Interruzione ricevuta. Uscita.")
            break


if __name__ == "__main__":
    main()
