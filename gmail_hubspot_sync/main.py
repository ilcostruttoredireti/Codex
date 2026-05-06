"""
Gmail → HubSpot contact sync daemon.

Polls Gmail inbox at a configurable interval, extracts sender info from each
new message, and creates or updates the corresponding HubSpot contact.

Usage:
    python -m gmail_hubspot_sync.main

Environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN   HubSpot private app token (required)
    GMAIL_CREDENTIALS_FILE Path to OAuth credentials JSON (default: credentials.json)
    GMAIL_TOKEN_FILE       Path to OAuth token cache (default: token.json)
    POLL_INTERVAL_SECONDS  Polling interval in seconds (default: 60)
    INITIAL_LOOKBACK_DAYS  Days to look back on first run (default: 1)
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import ContactResult, HubSpotClient
from .state import SyncState

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        logger.error("Missing required env var: %s", name)
        sys.exit(1)
    return val


def _initial_timestamp(lookback_days: int) -> int:
    """Return epoch-ms for `lookback_days` ago."""
    ts = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)
    return int(ts.timestamp() * 1000)


def _format_result(result: ContactResult) -> str:
    status_icon = {"created": "✚", "updated": "↻", "ignored": "—"}.get(result.status, "?")
    parts = [
        f"{status_icon} [{result.status.upper():8s}]",
        f"email={result.email}",
        f"id={result.contact_id or 'N/A'}",
    ]
    if result.reason:
        parts.append(f"({result.reason})")
    return "  ".join(parts)


def run_sync_cycle(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    state: SyncState,
) -> int:
    """Process all new emails since last poll. Returns count of emails processed."""
    after_ms = state.last_poll_ms
    seen_ids = state.seen_message_ids
    processed = 0
    now_ms = int(time.time() * 1000)

    for sender in gmail.fetch_new_senders(after_ms, seen_ids):
        processed += 1
        logger.info(
            "Processing email from %s (msg_id=%s, subject=%r)",
            sender.email,
            sender.message_id,
            sender.subject[:60] if sender.subject else "",
        )

        result = hubspot.sync_contact(
            email=sender.email,
            first_name=sender.first_name,
            last_name=sender.last_name,
            domain=sender.domain,
            subject=sender.subject,
            received_at_ms=sender.received_at,
        )

        print(_format_result(result))
        state.mark_seen(sender.message_id)

    state.update_poll_timestamp(now_ms)
    return processed


def main() -> None:
    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    lookback_days = int(os.getenv("INITIAL_LOOKBACK_DAYS", "1"))

    state = SyncState()
    gmail = GmailClient(credentials_file, token_file)
    hubspot = HubSpotClient(hubspot_token)

    logger.info("Authenticating with Gmail...")
    gmail.authenticate()

    # On first run, seed the timestamp so we don't scan all of history
    if state.last_poll_ms == 0:
        state.update_poll_timestamp(_initial_timestamp(lookback_days))
        logger.info(
            "First run — looking back %d day(s) for existing emails", lookback_days
        )

    logger.info(
        "Starting sync loop (poll interval: %ds). Press Ctrl+C to stop.", poll_interval
    )

    while True:
        try:
            count = run_sync_cycle(gmail, hubspot, state)
            if count:
                logger.info("Cycle complete — processed %d email(s)", count)
            else:
                logger.debug("No new emails found")
        except KeyboardInterrupt:
            logger.info("Shutting down")
            break
        except Exception as exc:
            logger.error("Unexpected error in sync cycle: %s", exc, exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
