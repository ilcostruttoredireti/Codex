"""
Entry point — Gmail → HubSpot contact sync.

Usage:
    python src/main.py [--dry-run] [--max 100] [--once]

Run continuously (default) or once with --once.
"""

import argparse
import logging
import time

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 300  # 5 minutes


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync Gmail senders → HubSpot contacts")
    p.add_argument("--dry-run", action="store_true", help="Parse & log without writing to HubSpot")
    p.add_argument("--max", type=int, default=100, metavar="N", help="Max messages per run (default 100)")
    p.add_argument("--once", action="store_true", help="Run once and exit (instead of polling)")
    p.add_argument("--credentials", default="credentials.json", help="Path to Google OAuth credentials")
    p.add_argument("--token", default="token.json", help="Path to cached OAuth token")
    return p.parse_args()


def run_once(engine: SyncEngine, max_messages: int) -> None:
    results = engine.run(max_messages=max_messages)
    created = sum(1 for r in results if r.status.value == "Creato")
    updated = sum(1 for r in results if r.status.value == "Aggiornato")
    skipped = sum(1 for r in results if r.status.value == "Ignorato")
    errors  = sum(1 for r in results if r.status.value == "Errore")
    logger.info(
        "Sync complete — processed: %d | creati: %d | aggiornati: %d | ignorati: %d | errori: %d",
        len(results), created, updated, skipped, errors,
    )


def main() -> None:
    args = parse_args()

    gmail = GmailClient(credentials_file=args.credentials, token_file=args.token)
    gmail.connect()

    hubspot = HubSpotClient()
    engine = SyncEngine(gmail=gmail, hubspot=hubspot, dry_run=args.dry_run)

    if args.once:
        run_once(engine, args.max)
        return

    logger.info("Starting continuous sync (polling every %ds) …", POLL_INTERVAL_SECONDS)
    while True:
        try:
            run_once(engine, args.max)
        except KeyboardInterrupt:
            logger.info("Interrupted — exiting.")
            break
        except Exception as exc:
            logger.error("Unexpected error: %s — retrying in %ds", exc, POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
