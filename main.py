"""
Gmail → HubSpot contact sync daemon.

Usage:
    python main.py [--interval SECONDS] [--no-activity]

Environment variables (or .env file):
    HUBSPOT_ACCESS_TOKEN   – HubSpot private-app access token  (required)
    GMAIL_CREDENTIALS_FILE – path to Google OAuth client JSON  (default: credentials.json)
    GMAIL_TOKEN_FILE       – path to stored OAuth token        (default: token.json)
    POLL_INTERVAL          – seconds between Gmail polls       (default: 60)
    LOG_LEVEL              – DEBUG / INFO / WARNING            (default: INFO)
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from contact_extractor import extract_contact
from gmail_monitor import GmailMonitor
from hubspot_sync import HubSpotSync, SyncStatus

load_dotenv()


# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

def _setup_logging(level_name: str = "INFO") -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=level,
    )


logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

_STATUS_ICON = {
    SyncStatus.CREATED: "✅",
    SyncStatus.UPDATED: "🔄",
    SyncStatus.IGNORED: "⚠️ ",
}


def _print_result(result) -> None:
    icon = _STATUS_ICON.get(result.status, "  ")
    cid = result.contact_id or "—"
    detail = f"  ({result.detail})" if result.detail else ""
    print(f"{icon}  Stato: {result.status.value:<10}  Email: {result.email:<40}  ID HubSpot: {cid}{detail}")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--interval", type=int, default=None, help="Poll interval in seconds")
    parser.add_argument("--no-activity", action="store_true", help="Skip timeline activity logging")
    args = parser.parse_args()

    _setup_logging(os.getenv("LOG_LEVEL", "INFO"))

    # --- Validate required config ---
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        logger.error("HUBSPOT_ACCESS_TOKEN is not set.  Aborting.")
        sys.exit(1)

    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    interval = args.interval or int(os.getenv("POLL_INTERVAL", "60"))
    log_activity = not args.no_activity

    # --- Authenticate ---
    monitor = GmailMonitor(credentials_file=credentials_file, token_file=token_file)
    monitor.authenticate()

    syncer = HubSpotSync(access_token=hubspot_token)

    logger.info(
        "Sync daemon avviato.  Poll interval: %ds  Timeline activity: %s",
        interval,
        "on" if log_activity else "off",
    )
    print("-" * 80)
    print(f"{'Stato':<14} {'Email':<42} {'ID HubSpot'}")
    print("-" * 80)

    # --- Main loop ---
    for message in monitor.poll(interval_seconds=interval):
        contact = extract_contact(message)
        if contact is None:
            logger.debug("Skipped message %s – could not extract sender.", message.get("id"))
            continue

        result = syncer.sync(contact, log_activity=log_activity)
        _print_result(result)


if __name__ == "__main__":
    main()
