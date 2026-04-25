#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls the Gmail INBOX and upserts senders as HubSpot contacts.
"""

import logging
import os
import time

from dotenv import load_dotenv

from contact_sync import ContactSyncer, SyncResult
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

load_dotenv()


def _setup_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _load_config() -> tuple[str, str, int, int]:
    token = os.getenv("HUBSPOT_API_TOKEN", "")
    creds = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    max_msgs = int(os.getenv("MAX_MESSAGES_PER_POLL", "50"))

    if not token:
        raise SystemExit("ERROR: HUBSPOT_API_TOKEN is not set in the environment / .env file")
    if not os.path.exists(creds):
        raise SystemExit(f"ERROR: Gmail credentials file not found: {creds}")

    return token, creds, interval, max_msgs


def _print_summary(results: list[SyncResult]):
    created  = sum(1 for r in results if r.status == "created")
    updated  = sum(1 for r in results if r.status == "updated")
    ignored  = sum(1 for r in results if r.status == "ignored")
    errors   = sum(1 for r in results if r.status == "error")
    print(
        f"  → Summary: {created} created  {updated} updated  "
        f"{ignored} ignored  {errors} errors"
    )
    for result in results:
        print(f"     {result}")


def main():
    _setup_logging()
    logger = logging.getLogger(__name__)
    logger.info("=== Gmail → HubSpot Contact Sync ===")

    token, creds_file, poll_interval, max_msgs = _load_config()

    logger.info("Authenticating with Gmail…")
    gmail = GmailClient(creds_file)

    logger.info("Connecting to HubSpot…")
    hubspot = HubSpotClient(token)

    syncer = ContactSyncer(gmail, hubspot)
    logger.info("Monitoring started  |  poll interval: %ds  |  max per cycle: %d", poll_interval, max_msgs)

    while True:
        try:
            logger.info("Checking for new emails…")
            messages = gmail.get_new_messages(max_results=max_msgs)

            if messages:
                logger.info("Processing %d new message(s)", len(messages))
                results = syncer.process_messages(messages)
                _print_summary(results)
            else:
                logger.debug("No new messages")

        except KeyboardInterrupt:
            logger.info("Shutting down — goodbye.")
            break
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
