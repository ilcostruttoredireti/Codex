"""
Entry point for the Gmail → HubSpot contact sync.

Usage:
    python main.py                   # single run
    python main.py --watch 300       # poll every 300 seconds (5 min)
    python main.py --dry-run         # parse only, no HubSpot writes

Environment variables (required unless --dry-run):
    HUBSPOT_API_KEY          HubSpot Private App access token
    GMAIL_CREDENTIALS_FILE   Path to Google OAuth2 credentials JSON  (default: credentials.json)
    GMAIL_TOKEN_FILE         Path to stored OAuth2 token              (default: token.json)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from models import SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

_BANNER = "=" * 60


def _print_result(result) -> None:
    icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭ "}.get(
        result.status.value, "❓"
    )
    id_part = f"  (HubSpot ID: {result.hubspot_id})" if result.hubspot_id else ""
    err_part = f"  ⚠  {result.error}" if result.error else ""
    print(f"  {icon} [{result.status.value}] {result.email}{id_part}{err_part}")


def run_once(dry_run: bool = False) -> dict:
    counts = {SyncStatus.CREATED: 0, SyncStatus.UPDATED: 0, SyncStatus.SKIPPED: 0}

    if dry_run:
        log.info("Modalità DRY-RUN — nessuna scrittura su HubSpot.")
        from gmail_client import GmailClient
        from parser import parse_from_header
        from models import SyncResult
        from sync import _should_skip

        gmail = GmailClient()
        for msg in gmail.iter_unprocessed_messages():
            contact = parse_from_header(msg.get("from", ""))
            if _should_skip(contact):
                status = SyncStatus.SKIPPED
            else:
                status = SyncStatus.CREATED  # hypothetical
            result = SyncResult(
                status=status,
                email=contact.email,
                message_id=msg["id"],
                subject=msg.get("subject", ""),
            )
            _print_result(result)
            counts[result.status] += 1
    else:
        from gmail_client import GmailClient
        from hubspot_client import HubSpotClient
        from sync import run_sync_batch

        gmail = GmailClient()
        hs = HubSpotClient()
        for result in run_sync_batch(gmail, hs):
            _print_result(result)
            counts[result.status] += 1

    return counts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza mittenti Gmail → HubSpot CRM"
    )
    parser.add_argument(
        "--watch",
        type=int,
        metavar="SECONDS",
        help="Esegui continuamente ogni SECONDS secondi (es. --watch 300)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Esegui solo il parsing Gmail senza scrivere su HubSpot",
    )
    args = parser.parse_args()

    print(_BANNER)
    print("  Gmail → HubSpot Contact Sync")
    print(_BANNER)

    if args.watch:
        log.info("Modalità watch: polling ogni %d secondi.", args.watch)
        cycle = 0
        while True:
            cycle += 1
            print(f"\n── Ciclo #{cycle} ──────────────────────────────")
            try:
                counts = run_once(dry_run=args.dry_run)
                log.info(
                    "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
                    counts[SyncStatus.CREATED],
                    counts[SyncStatus.UPDATED],
                    counts[SyncStatus.SKIPPED],
                )
            except KeyboardInterrupt:
                log.info("Interruzione manuale. Uscita.")
                sys.exit(0)
            except Exception as exc:
                log.error("Errore nel ciclo: %s", exc)
            time.sleep(args.watch)
    else:
        try:
            counts = run_once(dry_run=args.dry_run)
        except KeyboardInterrupt:
            log.info("Interruzione manuale.")
            sys.exit(0)

        print()
        print(_BANNER)
        print(
            f"  Riepilogo → "
            f"Creati: {counts[SyncStatus.CREATED]}  "
            f"Aggiornati: {counts[SyncStatus.UPDATED]}  "
            f"Ignorati: {counts[SyncStatus.SKIPPED]}"
        )
        print(_BANNER)


if __name__ == "__main__":
    main()
