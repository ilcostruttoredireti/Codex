#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Usage:
    python main.py              # single pass (latest 50 inbox emails)
    python main.py --watch      # continuous mode, polls every N seconds
    python main.py --max 100    # process up to 100 emails
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path
from typing import Set

from gmail_client import get_service, iter_inbox_messages, add_label
from sync_engine import sync_sender
from models import SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

PROCESSED_IDS_FILE = Path(".processed_ids")
GMAIL_LABEL = "HubSpot Synced"


def load_processed_ids() -> Set[str]:
    if PROCESSED_IDS_FILE.exists():
        return set(PROCESSED_IDS_FILE.read_text().splitlines())
    return set()


def save_processed_id(msg_id: str) -> None:
    with PROCESSED_IDS_FILE.open("a") as f:
        f.write(msg_id + "\n")


def run_once(service, max_results: int, processed_ids: Set[str]) -> dict:
    stats = {SyncStatus.CREATED: 0, SyncStatus.UPDATED: 0, SyncStatus.IGNORED: 0}
    results = []

    for msg in iter_inbox_messages(service, max_results=max_results):
        if msg["id"] in processed_ids:
            continue

        result = sync_sender(
            raw_from=msg["from"],
            subject=msg.get("subject", ""),
            snippet=msg.get("snippet", ""),
            date=msg.get("date", ""),
        )
        stats[result.status] += 1
        results.append(result)
        processed_ids.add(msg["id"])
        save_processed_id(msg["id"])

        try:
            add_label(service, msg["id"], GMAIL_LABEL)
        except Exception:
            pass

        _print_result(result)

    logger.info(
        "--- Riepilogo: Creati=%d  Aggiornati=%d  Ignorati=%d ---",
        stats[SyncStatus.CREATED],
        stats[SyncStatus.UPDATED],
        stats[SyncStatus.IGNORED],
    )
    return stats


def _print_result(result) -> None:
    icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭"}.get(result.status.value, "•")
    hs_id = f"  ID HubSpot: {result.hubspot_id}" if result.hubspot_id else ""
    print(f"{icon}  Stato: {result.status.value:<12}  Email: {result.contact_email}{hs_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--watch", action="store_true",
                        help="Modalità continua (polling)")
    parser.add_argument("--interval", type=int, default=300,
                        help="Secondi tra un ciclo e l'altro in modalità --watch (default 300)")
    parser.add_argument("--max", type=int, default=50, dest="max_results",
                        help="Numero massimo di email da processare per ciclo (default 50)")
    args = parser.parse_args()

    service = get_service()
    processed_ids = load_processed_ids()

    if args.watch:
        logger.info("Modalità watch attiva — polling ogni %ds", args.interval)
        while True:
            try:
                run_once(service, args.max_results, processed_ids)
            except KeyboardInterrupt:
                logger.info("Interruzione utente.")
                sys.exit(0)
            except Exception as exc:
                logger.error("Errore nel ciclo: %s", exc)
            time.sleep(args.interval)
    else:
        run_once(service, args.max_results, processed_ids)


if __name__ == "__main__":
    main()
