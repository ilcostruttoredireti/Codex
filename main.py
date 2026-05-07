"""
Gmail → HubSpot contact sync
-----------------------------
Polls Gmail for new inbox messages and upserts senders as HubSpot contacts.

Required environment variables (see .env.example):
  GMAIL_CREDENTIALS_FILE   path to OAuth2 client-secrets JSON
  HUBSPOT_ACCESS_TOKEN     HubSpot Private App token

Optional:
  GMAIL_TOKEN_FILE         where to cache the Gmail OAuth token (default: credentials/token.json)
  GMAIL_USER_ID            Gmail account to monitor (default: me)
  POLL_INTERVAL            seconds between polls (default: 30)
"""

import logging
import os
import time

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync_engine import SyncEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise SystemExit(f'Missing required environment variable: {key}')
    return value


def main() -> None:
    credentials_file = _require_env('GMAIL_CREDENTIALS_FILE')
    hubspot_token = _require_env('HUBSPOT_ACCESS_TOKEN')
    token_file = os.environ.get('GMAIL_TOKEN_FILE', 'credentials/token.json')
    user_id = os.environ.get('GMAIL_USER_ID', 'me')
    poll_interval = int(os.environ.get('POLL_INTERVAL', '30'))

    logger.info("Starting Gmail → HubSpot contact sync")

    gmail = GmailClient(credentials_file, token_file, user_id)
    hubspot = HubSpotClient(hubspot_token)
    engine = SyncEngine(gmail, hubspot)

    # Anchor at current history so we only process emails that arrive *after* startup
    gmail.initialize_history()
    logger.info("Polling every %ds — waiting for new messages…", poll_interval)

    while True:
        try:
            message_ids = gmail.get_new_message_ids()

            if message_ids:
                logger.info("Found %d new email(s)", len(message_ids))
                for msg_id in message_ids:
                    try:
                        result = engine.process_message(msg_id)
                        logger.info(str(result))
                    except Exception as exc:
                        logger.error("Failed to process message %s: %s", msg_id, exc)

        except KeyboardInterrupt:
            logger.info("Stopped by user")
            break
        except Exception as exc:
            logger.error("Polling error: %s", exc)

        time.sleep(poll_interval)


if __name__ == '__main__':
    main()
