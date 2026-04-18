"""
Gmail → HubSpot contact sync
─────────────────────────────
Continuously polls Gmail for new inbound emails, extracts sender info, and
creates or updates contacts in HubSpot.

Usage:
    python main.py

On first run an OAuth consent screen will open in your browser.
Subsequent runs reuse the stored token.
"""

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from contact_sync import ContactSync
from gmail_client import GmailClient, HistoryExpiredError
from hubspot_client import HubSpotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_USER_ID = os.getenv("GMAIL_USER_ID", "me")
HUBSPOT_TOKEN = os.getenv("HUBSPOT_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.getenv("STATE_FILE", "state.json")
IGNORED_DOMAINS = set(
    d.strip()
    for d in os.getenv(
        "IGNORED_DOMAINS",
        "gmail.com,yahoo.com,yahoo.it,hotmail.com,hotmail.it,"
        "outlook.com,live.com,icloud.com,me.com,protonmail.com,"
        "aol.com,libero.it,virgilio.it,tiscali.it",
    ).split(",")
    if d.strip()
)


# ── State persistence ──────────────────────────────────────────────────────────


def load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Main loop ──────────────────────────────────────────────────────────────────


def main() -> None:
    if not HUBSPOT_TOKEN:
        raise SystemExit("HUBSPOT_TOKEN is not set. Check your .env file.")
    if not Path(GMAIL_CREDENTIALS_FILE).exists():
        raise SystemExit(
            f"Gmail credentials file '{GMAIL_CREDENTIALS_FILE}' not found.\n"
            "Download it from Google Cloud Console and place it in the project root."
        )

    logger.info("Initialising Gmail client…")
    gmail = GmailClient(
        credentials_file=GMAIL_CREDENTIALS_FILE,
        token_file=GMAIL_TOKEN_FILE,
        user_id=GMAIL_USER_ID,
    )
    hubspot = HubSpotClient(HUBSPOT_TOKEN)
    syncer = ContactSync(
        hubspot=hubspot,
        ignored_domains=IGNORED_DOMAINS,
        add_timeline_note=True,
    )

    state = load_state()

    # Seed historyId on first run
    if "last_history_id" not in state:
        state["last_history_id"] = gmail.get_current_history_id()
        save_state(state)
        logger.info("Seeded historyId=%s — monitoring starts from now.", state["last_history_id"])

    logger.info(
        "Monitoring Gmail inbox every %ds. Ignored domains: %s",
        POLL_INTERVAL,
        ", ".join(sorted(IGNORED_DOMAINS)),
    )

    while True:
        try:
            _poll(gmail, syncer, state)
        except HistoryExpiredError:
            logger.warning("History ID expired — re-seeding from current position.")
            state["last_history_id"] = gmail.get_current_history_id()
            save_state(state)
        except KeyboardInterrupt:
            logger.info("Stopped.")
            break
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        time.sleep(POLL_INTERVAL)


def _poll(gmail: GmailClient, syncer: ContactSync, state: dict) -> None:
    history_id = state["last_history_id"]
    messages = gmail.get_new_messages(history_id)

    if not messages:
        logger.debug("No new messages.")
    else:
        logger.info("Processing %d new message(s)…", len(messages))

    seen_emails: set[str] = set()

    for msg in messages:
        email = msg["email"]

        # De-duplicate within the same batch
        if email in seen_emails:
            continue
        seen_emails.add(email)

        try:
            result = syncer.process(email, msg["name"], msg["subject"])
            logger.info(str(result))
        except Exception as exc:
            logger.error("Failed to sync %s: %s", email, exc)

    # Advance the cursor even when there are no messages
    new_history_id = gmail.get_latest_history_id(history_id)
    if new_history_id != history_id:
        state["last_history_id"] = new_history_id
        save_state(state)


if __name__ == "__main__":
    main()
