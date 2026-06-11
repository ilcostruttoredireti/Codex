"""
Main sync loop: polls Gmail for new messages, upserts senders into HubSpot.

Usage
-----
  python sync.py          # run once
  python sync.py --loop   # run continuously (respects POLL_INTERVAL_SECONDS)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

import config
from gmail_client import GmailClient, SenderInfo
from hubspot_client import HubSpotClient, SyncResult, SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _banner(text: str) -> None:
    width = 72
    logger.info("=" * width)
    logger.info(text.center(width))
    logger.info("=" * width)


def run_once(gmail: GmailClient, hubspot: HubSpotClient) -> list[SyncResult]:
    """Process all new inbox messages and return a list of sync results."""
    results: list[SyncResult] = []

    _banner(f"Sync run started  {datetime.now(timezone.utc).isoformat(timespec='seconds')}")

    for sender in gmail.fetch_new_messages():
        logger.info("Processing: %s (%s)", sender.full_name or sender.email, sender.email)

        try:
            result = hubspot.upsert_contact(
                email=sender.email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                company=sender.company,
            )
        except Exception as exc:
            logger.error("HubSpot error for %s: %s", sender.email, exc)
            continue

        # mark the Gmail message so we don't re-process it
        try:
            gmail.mark_processed(sender.message_id)
        except Exception as exc:
            logger.warning("Could not label message %s: %s", sender.message_id, exc)

        logger.info("  → %s", result)
        results.append(result)

    _summarise(results)
    return results


def _summarise(results: list[SyncResult]) -> None:
    if not results:
        logger.info("No new messages to process.")
        return

    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)

    logger.info("─" * 72)
    logger.info(
        "Summary: %d processed  |  %d created  |  %d updated  |  %d ignored",
        len(results), created, updated, ignored,
    )
    logger.info("─" * 72)

    for r in results:
        icon = {"CREATED": "✚", "UPDATED": "✎", "IGNORED": "─"}.get(r.status.name, "?")
        logger.info("  %s  %-40s  ID: %s", icon, r.contact_email, r.hubspot_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously, polling every POLL_INTERVAL_SECONDS seconds",
    )
    args = parser.parse_args()

    gmail = GmailClient()
    hs = HubSpotClient()

    if not args.loop:
        run_once(gmail, hs)
        return

    _banner("Continuous sync mode  (Ctrl-C to stop)")
    logger.info("Poll interval: %d seconds", config.POLL_INTERVAL_SECONDS)

    while True:
        try:
            run_once(gmail, hs)
        except KeyboardInterrupt:
            logger.info("Interrupted – exiting.")
            sys.exit(0)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        logger.info("Sleeping %d s …", config.POLL_INTERVAL_SECONDS)
        try:
            time.sleep(config.POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Interrupted – exiting.")
            sys.exit(0)


if __name__ == "__main__":
    main()
