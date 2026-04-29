"""
Gmail → HubSpot Contact Sync
=============================
Continuously monitors Gmail for incoming emails and syncs sender contacts
to HubSpot, creating new records or updating existing ones.

Usage:
    python main.py [--interval SECONDS] [--once]
"""

import argparse
import logging
import os
import time

from dotenv import load_dotenv

from contact_sync import ContactSyncer, SyncStatus
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

BANNER = """
╔══════════════════════════════════════════════╗
║       Gmail → HubSpot Contact Sync           ║
║  Monitoring inbox and syncing new contacts   ║
╚══════════════════════════════════════════════╝
"""


def build_syncer() -> ContactSyncer:
    hubspot_key = os.environ.get("HUBSPOT_API_KEY")
    if not hubspot_key:
        raise SystemExit("HUBSPOT_API_KEY environment variable is required.")

    credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")

    gmail = GmailClient(credentials_file=credentials_file, token_file=token_file)
    hubspot = HubSpotClient(api_key=hubspot_key)

    timeline_app_id = os.environ.get("HUBSPOT_TIMELINE_APP_ID")
    timeline_template_id = os.environ.get("HUBSPOT_TIMELINE_TEMPLATE_ID")
    log_timeline = bool(timeline_app_id and timeline_template_id)

    return ContactSyncer(
        gmail,
        hubspot,
        ignore_free_email=os.environ.get("IGNORE_FREE_EMAIL", "true").lower() == "true",
        log_timeline=log_timeline,
        timeline_app_id=int(timeline_app_id) if timeline_app_id else None,
        timeline_template_id=timeline_template_id,
    )


def print_summary(results: list) -> None:
    if not results:
        return

    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)

    print("\n--- Riepilogo ---")
    for r in results:
        icon = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–"}.get(r.status.value, " ")
        contact_info = f" [id={r.contact_id}]" if r.contact_id else ""
        print(f"  {icon} [{r.status.value}] {r.email}{contact_info}")

    print(f"\n  Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {ignored}\n")


def run_loop(syncer: ContactSyncer, interval: int) -> None:
    logger.info("Polling every %d seconds. Press Ctrl+C to stop.", interval)
    while True:
        try:
            results = syncer.run_once()
            print_summary(results)
        except KeyboardInterrupt:
            logger.info("Shutting down.")
            break
        except Exception:
            logger.exception("Error during sync cycle — will retry next interval.")
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync daemon")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle and exit",
    )
    args = parser.parse_args()

    print(BANNER)

    syncer = build_syncer()

    if args.once:
        results = syncer.run_once()
        print_summary(results)
    else:
        run_loop(syncer, args.interval)


if __name__ == "__main__":
    main()
