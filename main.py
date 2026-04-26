"""
Gmail → HubSpot contact sync.

Continuously polls Gmail for new incoming messages and syncs sender
contacts to HubSpot CRM. Run with:

    python main.py

On first run you will be prompted to authenticate with Google in your
browser. The OAuth token is then cached locally for subsequent runs.
"""

import logging
import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from contact_sync import sync_message, SyncStatus
from state_manager import StateManager

# ------------------------------------------------------------------
# Bootstrap
# ------------------------------------------------------------------

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


# ------------------------------------------------------------------
# Single poll cycle
# ------------------------------------------------------------------

def run_cycle(gmail: GmailClient, hubspot: HubSpotClient, state: StateManager) -> None:
    logger.info("Checking for new Gmail messages (history_id=%s)…", state.history_id)

    messages, new_history_id = gmail.get_new_messages(state.history_id)

    if not messages:
        logger.info("No new messages found.")
    else:
        logger.info("Processing %d message(s)…", len(messages))

    counts = {s: 0 for s in SyncStatus}

    for message in messages:
        msg_id = message.get("id", "")

        if state.is_processed(msg_id):
            logger.debug("Message %s already processed — skipping.", msg_id)
            continue

        result = sync_message(message, gmail, hubspot)
        state.mark_processed(msg_id)
        counts[result.status] += 1

        _print_result(result)

    if messages:
        _print_summary(counts)

    # Always advance the history ID so the next poll is incremental
    state.history_id = new_history_id


def _print_result(result) -> None:
    line = str(result)
    if result.status == SyncStatus.ERROR:
        logger.error(line)
    elif result.status == SyncStatus.SKIPPED:
        logger.debug(line)
    else:
        logger.info(line)
    # Always print to stdout so the user can pipe/redirect the output
    print(line)


def _print_summary(counts: dict) -> None:
    parts = [f"{status.value}: {n}" for status, n in counts.items() if n]
    if parts:
        print(f"\n--- Riepilogo: {' | '.join(parts)} ---\n")


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def main() -> None:
    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials/gmail_credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "credentials/gmail_token.json")
    gmail_user = os.getenv("GMAIL_USER", "me")
    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))
    state_file = os.getenv("STATE_FILE", "state/sync_state.json")

    logger.info("=== Gmail → HubSpot Contact Sync ===")
    logger.info("Poll interval: %d seconds", poll_interval)

    gmail = GmailClient(
        credentials_file=credentials_file,
        token_file=token_file,
        user=gmail_user,
    )
    hubspot = HubSpotClient(access_token=hubspot_token)
    state = StateManager(state_file=state_file)

    logger.info("Authentication successful. Starting sync loop…")

    while True:
        try:
            run_cycle(gmail, hubspot, state)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as e:
            logger.error("Unhandled error in poll cycle: %s", e, exc_info=True)

        logger.debug("Sleeping %d seconds until next poll…", poll_interval)
        try:
            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break


if __name__ == "__main__":
    main()
