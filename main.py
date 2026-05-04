#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Continuously monitors Gmail for incoming emails and upserts sender
contacts into HubSpot, deduplicating by email address.

Usage
-----
    python main.py            # continuous monitor
    python main.py --once     # single pass then exit
    python main.py --dry-run  # parse Gmail but skip HubSpot writes

Environment
-----------
Set variables in a .env file (see .env.example) or export them directly.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.sync import GmailHubSpotSync

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        log.error("Missing required environment variable: %s", key)
        sys.exit(1)
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync pass then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch emails but do not write to HubSpot",
    )
    parser.add_argument(
        "--query",
        default=os.getenv("GMAIL_QUERY", "is:unread in:inbox"),
        help="Gmail search query (default: is:unread in:inbox)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL", "60")),
        help="Seconds between polling passes (default: 60)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")

    if not Path(credentials_file).exists():
        log.error(
            "Gmail credentials file not found: %s\n"
            "Download it from the Google Cloud Console and place it here.",
            credentials_file,
        )
        sys.exit(1)

    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")

    ignored_raw = os.getenv("IGNORED_DOMAINS", "")
    ignored_domains = {d.strip().lower() for d in ignored_raw.split(",") if d.strip()}

    log.info("Initialising Gmail client…")
    gmail = GmailClient(credentials_file, token_file)

    if args.dry_run:
        log.info("DRY-RUN mode: HubSpot writes are disabled.")
        hubspot = _DryRunHubSpot()
    else:
        log.info("Initialising HubSpot client…")
        hubspot = HubSpotClient(hubspot_token)

    syncer = GmailHubSpotSync(
        gmail=gmail,
        hubspot=hubspot,
        ignored_domains=ignored_domains,
        mark_read=not args.dry_run,
    )

    if args.once:
        results = syncer.run_once(args.query)
        if not results:
            log.info("No new messages found.")
        _print_table(results)
    else:
        syncer.run_forever(args.query, args.interval)


# ---------------------------------------------------------------------------
# Dry-run stub
# ---------------------------------------------------------------------------

class _DryRunHubSpot:
    """Pretends to upsert contacts without touching HubSpot."""

    def upsert_contact(self, sender: dict) -> tuple[str, str]:
        log.info("[dry-run] Would upsert: %s", sender["email"])
        return "ignored", "dry-run"


# ---------------------------------------------------------------------------
# Output table
# ---------------------------------------------------------------------------

_STATUS_COLOUR = {
    "created": "\033[32m",   # green
    "updated": "\033[33m",   # yellow
    "ignored": "\033[90m",   # grey
    "skipped": "\033[90m",   # grey
    "error":   "\033[31m",   # red
}
_RESET = "\033[0m"


def _print_table(results) -> None:
    if not results:
        return
    header = f"{'STATUS':<10} {'EMAIL':<40} {'HUBSPOT ID'}"
    print("\n" + header)
    print("-" * len(header))
    for r in results:
        colour = _STATUS_COLOUR.get(r.status, "")
        print(f"{colour}{r.status:<10}{_RESET} {r.email:<40} {r.contact_id or '-'}")
    print()


if __name__ == "__main__":
    main()
