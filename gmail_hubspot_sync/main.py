"""
Gmail → HubSpot contact sync.

Polls the Gmail INBOX continuously, extracts sender data from new messages,
and creates / updates HubSpot contacts accordingly.

Usage:
    python main.py
"""

import logging
import signal
import time

from config import POLL_INTERVAL_SECONDS
from contact_processor import ContactProcessor, SyncStatus
from gmail_monitor import GmailMonitor
from hubspot_client import HubSpotClient
from utils import setup_logging

logger = setup_logging()

_running = True


def _handle_sigterm(signum, frame):  # noqa: ANN001
    global _running
    logger.info("Received signal %s — shutting down gracefully.", signum)
    _running = False


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def run() -> None:
    gmail = GmailMonitor()
    hubspot = HubSpotClient()
    processor = ContactProcessor(hubspot)

    logger.info(
        "Gmail→HubSpot sync started. Polling every %ds. Press Ctrl+C to stop.",
        POLL_INTERVAL_SECONDS,
    )

    while _running:
        try:
            for message_id, raw_from in gmail.poll_new_senders():
                result = processor.process(raw_from)

                # ── Output ──────────────────────────────────────────────
                contact_id_str = result.contact_id or "—"
                reason_str = f"  ({result.reason})" if result.reason else ""
                logger.info(
                    "%-12s | %-40s | ID: %s%s",
                    result.status.value,
                    result.email,
                    contact_id_str,
                    reason_str,
                )

                # Mark the Gmail message as processed (apply label)
                if result.status in (SyncStatus.CREATED, SyncStatus.UPDATED):
                    gmail.mark_as_processed(message_id)

        except Exception as exc:
            logger.error("Unexpected error during poll cycle: %s", exc, exc_info=True)

        if _running:
            time.sleep(POLL_INTERVAL_SECONDS)

    logger.info("Sync stopped.")


if __name__ == "__main__":
    run()
