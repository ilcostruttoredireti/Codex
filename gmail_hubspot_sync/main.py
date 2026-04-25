"""
Main entry point: continuous monitoring loop.

Run with:
    python -m gmail_hubspot_sync.main

The loop polls Gmail every POLL_INTERVAL seconds (default 60).
Each new inbound email triggers a HubSpot contact create/update.
Results are printed to stdout in a structured format and logged.

Output per email:
  [CREATED]  user@example.com  →  HubSpot ID 12345
  [UPDATED]  user@example.com  →  HubSpot ID 12345
  [SKIPPED]  user@example.com  →  HubSpot ID 12345 (no new data)
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime

# Load .env if present (no-op when python-dotenv is absent or file missing)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from .config import load_config
from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import HubSpotClient
from .contact_sync import ContactSyncer, SyncResult

# ------------------------------------------------------------------
# Logging setup
# ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gmail_hubspot_sync")


# ------------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------------

_running = True


def _handle_signal(sig, frame):  # noqa: ANN001
    global _running
    logger.info("Shutdown signal received. Stopping after current poll.")
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ------------------------------------------------------------------
# Output formatting
# ------------------------------------------------------------------

_STATUS_LABELS = {
    "created": "CREATED",
    "updated": "UPDATED",
    "skipped": "SKIPPED",
}


def _print_result(result: SyncResult) -> None:
    label = _STATUS_LABELS.get(result.status, result.status.upper())
    ts = datetime.now().strftime("%H:%M:%S")
    note = ""
    if result.status == "skipped":
        note = " (no new data)"
    print(
        f"[{ts}]  [{label:7s}]  {result.email:<40}  →  HubSpot ID {result.hubspot_id}{note}",
        flush=True,
    )


# ------------------------------------------------------------------
# Main loop
# ------------------------------------------------------------------

def run() -> None:
    config = load_config()

    if not config.hubspot_access_token:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN environment variable is not set. Aborting."
        )
        sys.exit(1)

    gmail = GmailClient(config)
    gmail.authenticate()

    hs = HubSpotClient(config.hubspot_access_token)
    syncer = ContactSyncer(hs, config)

    logger.info(
        "Starting Gmail → HubSpot sync loop (poll interval: %ds)", config.poll_interval
    )
    print("=" * 70)
    print(f"  Gmail → HubSpot Sync  |  Polling every {config.poll_interval}s")
    print("=" * 70, flush=True)

    poll_count = 0

    while _running:
        poll_count += 1
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info("Poll #%d started at %s", poll_count, ts)

        processed = 0
        errors = 0

        try:
            for sender in gmail.poll_new_senders():
                try:
                    result = syncer.sync(sender)
                    _print_result(result)
                    processed += 1
                except Exception as exc:
                    logger.error(
                        "Error syncing %s: %s", sender.email, exc, exc_info=True
                    )
                    errors += 1
        except Exception as exc:
            logger.error("Error during Gmail poll: %s", exc, exc_info=True)

        if processed or errors:
            logger.info(
                "Poll #%d done — processed=%d errors=%d",
                poll_count, processed, errors,
            )
        else:
            logger.debug("Poll #%d — no new emails.", poll_count)

        # Wait for next poll, checking shutdown flag every second
        for _ in range(config.poll_interval):
            if not _running:
                break
            time.sleep(1)

    logger.info("Sync loop stopped.")


if __name__ == "__main__":
    run()
