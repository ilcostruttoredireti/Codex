#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Continuously polls the Gmail inbox for new messages and upserts sender
contacts into HubSpot, avoiding duplicates and back-filling missing fields.

Output per processed email
──────────────────────────
  [Creato | Aggiornato | Ignorato] <email>  →  HubSpot ID: <id>
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

from src.contact_extractor import extract_contact
from src.gmail_monitor import GmailMonitor
from src.hubspot_sync import HubSpotSync

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
GMAIL_CREDS = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN = os.getenv("GMAIL_TOKEN_FILE", "token.json")


def _check_config() -> None:
    if not HUBSPOT_TOKEN:
        sys.exit(
            "ERROR: HUBSPOT_ACCESS_TOKEN is not set.\n"
            "Create a HubSpot Private App and add the token to your .env file."
        )


def _process_batch(gmail: GmailMonitor, hubspot: HubSpotSync) -> None:
    emails = gmail.fetch_new_emails()

    for msg in emails:
        contact = extract_contact(msg)
        if contact is None:
            logger.debug("Skipped message %s (no usable sender)", msg.get("id"))
            continue

        try:
            status, contact_id = hubspot.sync_contact(contact)
        except Exception as exc:
            logger.error("Sync failed for %s: %s", contact["email"], exc)
            continue

        # ── Main output line ──────────────────────────────────────────
        print(
            f"[{status:<10}]  {contact['email']:<40}  →  HubSpot ID: {contact_id}"
        )

    gmail.save_state()


def main() -> None:
    _check_config()

    logger.info("Initialising Gmail monitor…")
    gmail = GmailMonitor(credentials_file=GMAIL_CREDS, token_file=GMAIL_TOKEN)

    logger.info("Initialising HubSpot sync…")
    hubspot = HubSpotSync(access_token=HUBSPOT_TOKEN)

    logger.info(
        "Gmail → HubSpot sync started  |  poll interval: %ds  |  press Ctrl-C to stop",
        POLL_INTERVAL,
    )

    while True:
        try:
            _process_batch(gmail, hubspot)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            break
        except Exception as exc:
            logger.error("Unexpected error in sync loop: %s", exc, exc_info=True)

        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            logger.info("Stopped by user")
            break


if __name__ == "__main__":
    main()
