#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora la casella Gmail e sincronizza automaticamente i mittenti come contatti HubSpot.
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from src.gmail_client import GmailClient
from src.hubspot_client import HubSpotClient
from src.sync_engine import run_sync_loop

load_dotenv()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"ERROR: missing required env variable: {name}", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--no-activity",
        action="store_true",
        help="Skip creating HubSpot timeline activities",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    parser.add_argument(
        "--credentials",
        default=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        help="Path to Gmail OAuth2 credentials JSON file",
    )
    parser.add_argument(
        "--token",
        default=os.getenv("GMAIL_TOKEN_FILE", "token.pickle"),
        help="Path to stored Gmail token (created on first run)",
    )
    args = parser.parse_args()

    _setup_logging(args.log_level)
    logger = logging.getLogger(__name__)

    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")

    logger.info("Initializing Gmail client...")
    gmail = GmailClient(
        credentials_file=args.credentials,
        token_file=args.token,
    )

    logger.info("Initializing HubSpot client...")
    hubspot = HubSpotClient(access_token=hubspot_token)

    logger.info(
        "Starting sync loop (poll interval: %ds, activities: %s)",
        args.interval,
        not args.no_activity,
    )

    run_sync_loop(
        gmail=gmail,
        hubspot_client=hubspot,
        poll_interval=args.interval,
        create_activity=not args.no_activity,
    )


if __name__ == "__main__":
    main()
