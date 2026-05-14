"""
Gmail → HubSpot contact sync — entry point.

Usage:
    python main.py                   # run once then exit
    python main.py --loop            # poll continuously (default: every 60 s)
    python main.py --loop --interval 120

Environment variables (or .env file):
    HUBSPOT_ACCESS_TOKEN    Required. HubSpot Private App access token.
    HUBSPOT_TIMELINE_APP_ID Optional. App ID for timeline event creation.
    GMAIL_CREDENTIALS_PATH  Path to credentials.json (default: credentials.json)
    GMAIL_TOKEN_PATH        Path to token.json (default: token.json)
    GMAIL_HISTORY_FILE      Path for persisting Gmail history ID (default: .gmail_history_id)
    SKIP_DOMAINS            Comma-separated list of domains to skip (e.g. mycompany.com)
    LOG_LEVEL               Logging level: DEBUG / INFO / WARNING (default: INFO)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

# Load .env if python-dotenv is available (optional dependency)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from gmail_client import GmailClient, SenderInfo
from hubspot_client import HubSpotClient, SyncResult, SyncStatus
from sync import GmailHubSpotSync, SyncStats

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pretty output callback
# ---------------------------------------------------------------------------

def _print_result(sender: SenderInfo, result: SyncResult) -> None:
    status_label = result.status.value.upper().ljust(10)
    name_part = f" ({sender.full_name})" if sender.full_name else ""
    company_part = f" | {sender.company}" if sender.company else ""
    print(
        f"  [{status_label}]  {result.email}{name_part}{company_part}"
        f"  →  ID: {result.contact_id}"
    )


def _print_stats(stats: SyncStats) -> None:
    print(
        f"\n  Riepilogo: "
        f"{stats.created} creati, "
        f"{stats.updated} aggiornati, "
        f"{stats.ignored} ignorati, "
        f"{stats.errors} errori"
        f"  (totale: {stats.total})\n"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_syncer() -> GmailHubSpotSync:
    access_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not access_token:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN non impostato. "
            "Crea un Private App su HubSpot e copia il token in .env"
        )
        sys.exit(1)

    skip_raw = os.getenv("SKIP_DOMAINS", "")
    skip_domains = {d.strip().lower() for d in skip_raw.split(",") if d.strip()}

    gmail = GmailClient()
    hubspot = HubSpotClient(
        access_token=access_token,
        timeline_app_id=os.getenv("HUBSPOT_TIMELINE_APP_ID"),
    )

    return GmailHubSpotSync(
        gmail=gmail,
        hubspot=hubspot,
        on_result=_print_result,
        skip_domains=skip_domains,
    )


def run(loop: bool, interval: int) -> None:
    syncer = build_syncer()

    logger.info("Gmail → HubSpot sync avviato (modalità: %s)", "loop" if loop else "singola esecuzione")
    if loop:
        logger.info("Intervallo di polling: %d secondi", interval)

    while True:
        print(f"\n--- Scansione email in corso ---")
        try:
            stats = syncer.run_once()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error("Errore durante la scansione: %s", exc, exc_info=True)
            stats = None

        if stats is not None:
            if stats.total == 0:
                print("  Nessuna nuova email trovata.")
            else:
                _print_stats(stats)

        if not loop:
            break

        logger.info("Prossima scansione tra %d secondi…", interval)
        time.sleep(interval)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti delle email Gmail con HubSpot CRM"
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Esegui in modalità continua (polling)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SECONDI",
        help="Secondi tra una scansione e la successiva in modalità --loop (default: 60)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    try:
        run(loop=args.loop, interval=args.interval)
    except KeyboardInterrupt:
        print("\nSync interrotto dall'utente.")
