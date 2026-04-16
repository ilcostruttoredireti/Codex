"""
Gmail → HubSpot contact sync — main entry point.

Run once:
    python main.py

Run continuously (poll every N seconds):
    python main.py --watch --interval 60
"""

from __future__ import annotations

import argparse
import logging
import os
import time
from typing import Optional

from dotenv import load_dotenv

from gmail_monitor import GmailMonitor, ProcessedStore
from hubspot_sync import HubSpotSync, SyncResult, SyncStatus

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


def _print_result(r: SyncResult) -> None:
    icons = {
        SyncStatus.CREATED: "✅ Creato  ",
        SyncStatus.UPDATED: "🔄 Aggiornato",
        SyncStatus.IGNORED: "⏭  Ignorato",
    }
    hs_id = r.hubspot_id or "—"
    detail = f"  ({r.detail})" if r.detail else ""
    print(f"  {icons[r.status]} | {r.email:<40} | ID: {hs_id:<12}{detail}")


def run_once(
    gmail: GmailMonitor,
    syncer: HubSpotSync,
    store: ProcessedStore,
    max_threads: int,
    gmail_query: str,
) -> list[SyncResult]:
    contacts = gmail.fetch_inbox_senders(
        max_results=max_threads,
        processed_ids=store.ids,
        query=gmail_query,
    )

    if not contacts:
        logger.info("Nessuna nuova email da processare.")
        return []

    print(f"\n📬 {len(contacts)} nuova/e email trovata/e — inizio sincronizzazione HubSpot...\n")
    results = syncer.sync_batch(contacts)

    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)

    print(f"{'Stato':<14} {'Email mittente':<42} {'HubSpot ID'}")
    print("-" * 80)
    for r in results:
        _print_result(r)
    print("-" * 80)
    print(f"  Riepilogo → ✅ Creati: {created}  🔄 Aggiornati: {updated}  ⏭ Ignorati: {ignored}\n")

    # Persist processed thread IDs
    for c in contacts:
        if c.thread_id:
            store.add(c.thread_id)
    store.save()

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Sincronizza contatti Gmail → HubSpot")
    parser.add_argument("--watch", action="store_true", help="Modalità monitoraggio continuo")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("SYNC_INTERVAL_SECONDS", "60")),
        help="Secondi tra un ciclo e l'altro (default: 60)",
    )
    parser.add_argument(
        "--max-threads",
        type=int,
        default=int(os.getenv("MAX_THREADS_PER_RUN", "50")),
        help="Numero massimo di thread per ciclo",
    )
    parser.add_argument(
        "--query",
        default="in:inbox",
        help='Query Gmail (es. "in:inbox is:unread")',
    )
    args = parser.parse_args()

    hubspot_token: Optional[str] = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise SystemExit("Errore: HUBSPOT_ACCESS_TOKEN non impostato in .env")

    gmail = GmailMonitor(
        credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
    )
    gmail.authenticate()

    syncer = HubSpotSync(access_token=hubspot_token)
    store = ProcessedStore(os.getenv("PROCESSED_IDS_FILE", "processed_threads.json"))

    if args.watch:
        print(f"🔍 Monitoraggio attivo — controllo ogni {args.interval}s. Premi Ctrl+C per fermare.\n")
        try:
            while True:
                run_once(gmail, syncer, store, args.max_threads, args.query)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n👋 Monitoraggio interrotto.")
    else:
        run_once(gmail, syncer, store, args.max_threads, args.query)


if __name__ == "__main__":
    main()
