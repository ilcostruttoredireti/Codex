"""
Gmail → HubSpot contact sync
============================
Polls the Gmail INBOX via the History API and upserts each sender
as a HubSpot contact, avoiding duplicates.

Usage
-----
  python main.py

Environment variables (see .env.example):
  GMAIL_CREDENTIALS_FILE  path to OAuth2 client-secrets JSON  (default: credentials.json)
  GMAIL_TOKEN_FILE        path to stored token                 (default: token.json)
  HUBSPOT_ACCESS_TOKEN    HubSpot Private App token            (required)
  POLL_INTERVAL_SECONDS   seconds between Gmail polls          (default: 60)
  STATE_FILE              JSON file for persisting state       (default: sync_state.json)
  LOG_LEVEL               Python logging level                 (default: INFO)
"""

from __future__ import annotations

import logging
import sys
import time

import config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import StateManager
from sync_manager import SyncManager

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def main() -> None:
    if not config.HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN is not set. Aborting.")
        sys.exit(1)

    logger.info("Initialising Gmail client…")
    gmail = GmailClient(config.GMAIL_CREDENTIALS_FILE, config.GMAIL_TOKEN_FILE)

    logger.info("Initialising HubSpot client…")
    hubspot = HubSpotClient(config.HUBSPOT_ACCESS_TOKEN)

    sync = SyncManager(gmail, hubspot)
    state = StateManager(config.STATE_FILE)

    # Bootstrap: record the current historyId so we only process future mail
    if not state.get_history_id():
        history_id = gmail.get_current_history_id()
        state.set_history_id(history_id)
        logger.info("Bootstrapped with historyId=%s (future mail only)", history_id)

    logger.info(
        "Polling every %ds. Press Ctrl-C to stop.", config.POLL_INTERVAL_SECONDS
    )

    processed = state.get_processed_messages()

    while True:
        try:
            history_id = state.get_history_id()
            new_ids, latest_history_id = gmail.poll_new_inbox_messages(history_id)

            if new_ids:
                logger.info("Found %d new inbox message(s)", len(new_ids))

            for msg_id in new_ids:
                if msg_id in processed:
                    continue

                result = sync.process_message(msg_id)
                print(result)

                state.mark_processed(msg_id)
                processed.add(msg_id)

            state.set_history_id(latest_history_id)

        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as exc:
            logger.error("Sync cycle error: %s", exc, exc_info=True)

        time.sleep(config.POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
