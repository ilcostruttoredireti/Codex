#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Run:  python main.py
Stop: Ctrl+C
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv

import state as store
from gmail_client import GmailClient, HistoryExpiredError
from hubspot_client import HubSpotClient
from sync_engine import SyncEngine, SyncResult

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

_STATUS_ICON = {
    'created': '✓ CREATED ',
    'updated': '↑ UPDATED ',
    'ignored': '– IGNORED ',
    'error':   '✗ ERROR   ',
}


def _log_result(result: SyncResult):
    icon = _STATUS_ICON.get(result.status, '? UNKNOWN ')
    parts = [icon, f"email={result.email or '(none)'}"]
    if result.contact_id:
        parts.append(f"hs_id={result.contact_id}")
    if result.detail:
        parts.append(f"| {result.detail}")
    logger.info(' '.join(parts))


def run():
    hubspot_token = os.getenv('HUBSPOT_ACCESS_TOKEN')
    if not hubspot_token:
        logger.error("HUBSPOT_ACCESS_TOKEN not set. Copy .env.example to .env and fill it in.")
        sys.exit(1)

    poll_interval = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))
    credentials_file = os.getenv('GMAIL_CREDENTIALS_FILE', 'credentials.json')
    token_file = os.getenv('GMAIL_TOKEN_FILE', 'token.json')

    gmail = GmailClient(credentials_file=credentials_file, token_file=token_file)
    gmail.authenticate()
    logger.info("Gmail authenticated successfully")

    hubspot = HubSpotClient(hubspot_token)
    sync = SyncEngine(hubspot)

    history_id = store.get_history_id()
    if not history_id:
        history_id = gmail.get_initial_history_id()
        store.set_history_id(history_id)
        logger.info("First run – saved baseline historyId=%s", history_id)
    else:
        logger.info("Resuming from historyId=%s", history_id)

    logger.info("Polling every %ds. Press Ctrl+C to stop.", poll_interval)

    while True:
        try:
            messages = gmail.list_new_messages(history_id)

            if messages:
                logger.info("Processing %d new message(s)…", len(messages))
                for stub in messages:
                    full_msg = gmail.get_message(stub['id'])
                    result = sync.process_email(full_msg)
                    _log_result(result)

            # Advance the watermark even when there were no new messages
            history_id = gmail.get_current_history_id()
            store.set_history_id(history_id)

        except HistoryExpiredError:
            logger.warning("History ID expired – resetting watermark to current")
            history_id = gmail.get_initial_history_id()
            store.set_history_id(history_id)

        except KeyboardInterrupt:
            logger.info("Interrupted by user, shutting down.")
            sys.exit(0)

        except Exception:
            logger.exception("Unexpected error during polling cycle")

        time.sleep(poll_interval)


if __name__ == '__main__':
    run()
