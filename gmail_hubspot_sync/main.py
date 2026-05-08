"""
Gmail → HubSpot contact sync
=============================
Continuously polls Gmail for new inbox messages, extracts sender data,
and creates or updates matching contacts in HubSpot.

Usage
-----
    python main.py

Required environment variables (see .env.example):
    HUBSPOT_API_TOKEN   – HubSpot Private App token
    GMAIL_CREDENTIALS   – path to Google OAuth credentials JSON  (default: credentials.json)
    GMAIL_TOKEN         – path where the OAuth token is cached    (default: token.json)
    STATE_FILE          – path to sync state file                 (default: sync_state.json)
    POLL_INTERVAL       – seconds between Gmail checks            (default: 60)
    ADD_NOTES           – attach activity note to contact         (default: true)
"""

import logging
import os
import sys
import time
from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from contact_processor import process_message
from state_manager import StateManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        logger.error("Environment variable %s is required but not set.", name)
        sys.exit(1)
    return value


def main() -> None:
    hubspot_token = _require_env("HUBSPOT_API_TOKEN")
    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))
    add_notes = os.getenv("ADD_NOTES", "true").lower() not in ("false", "0", "no")
    credentials_file = os.getenv("GMAIL_CREDENTIALS", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN", "token.json")
    state_file = os.getenv("STATE_FILE", "sync_state.json")

    logger.info("Initialising Gmail client…")
    gmail = GmailClient(credentials_file=credentials_file, token_file=token_file)

    logger.info("Initialising HubSpot client…")
    hubspot = HubSpotClient(api_token=hubspot_token)

    state = StateManager(path=state_file)

    logger.info(
        "Sync started — polling every %ds  |  add_notes=%s",
        poll_interval,
        add_notes,
    )

    # On the very first run, anchor the history ID to *now* so we only process
    # messages that arrive after this moment.  Remove this guard if you want to
    # back-fill existing inbox messages on first launch.
    first_run = state.last_history_id is None
    if first_run:
        current_id = gmail.get_current_history_id()
        state.last_history_id = current_id
        logger.info(
            "First run — history ID anchored at %s. "
            "Messages already in the inbox will NOT be back-filled. "
            "Delete sync_state.json to change this behaviour.",
            current_id,
        )

    while True:
        try:
            _run_cycle(gmail, hubspot, state, add_notes)
        except KeyboardInterrupt:
            logger.info("Interrupted by user. Bye!")
            break
        except Exception as exc:
            logger.exception("Unexpected error in sync cycle: %s", exc)

        time.sleep(poll_interval)


def _run_cycle(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    state: StateManager,
    add_notes: bool,
) -> None:
    message_ids = gmail.get_new_message_ids(state.last_history_id)

    # Update history anchor before processing so a crash doesn't re-fetch
    new_history_id = gmail.get_current_history_id()

    unprocessed = [mid for mid in message_ids if not state.is_processed(mid)]

    if not unprocessed:
        logger.debug("No new messages.")
        state.last_history_id = new_history_id
        return

    logger.info("Found %d new message(s) to process.", len(unprocessed))

    counts = {"CREATO": 0, "AGGIORNATO": 0, "IGNORATO": 0}

    for msg_id in unprocessed:
        try:
            result = process_message(msg_id, gmail, hubspot, add_notes=add_notes)
            state.mark_processed(msg_id)
            counts[result.status] += 1
            logger.info("%s", result)
        except Exception as exc:
            logger.error("Error processing message %s: %s", msg_id, exc)

    logger.info(
        "Cycle complete — Creati: %d  Aggiornati: %d  Ignorati: %d",
        counts["CREATO"],
        counts["AGGIORNATO"],
        counts["IGNORATO"],
    )

    state.last_history_id = new_history_id


if __name__ == "__main__":
    main()
