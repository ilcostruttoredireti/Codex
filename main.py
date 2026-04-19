#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Continuously monitors incoming Gmail messages and upserts sender contacts
into HubSpot CRM.  State is persisted in SQLite so restarts are seamless.

Usage:
    cp .env.example .env          # fill in your credentials
    pip install -r requirements.txt
    python main.py                 # starts the polling loop
    python main.py --once          # process pending messages once, then exit
"""

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass

from dotenv import load_dotenv

from src.contact_extractor import extract_contact
from src.gmail_monitor import GmailMonitor
from src.hubspot_client import HubSpotClient
from src.state_store import StateStore

load_dotenv()

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------

GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
GMAIL_USER_EMAIL = os.getenv("GMAIL_USER_EMAIL", "me")
HUBSPOT_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "sync_state.db")

# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

STATUS_ICONS = {"created": "✚", "updated": "↺", "ignored": "–"}


def _print_result(status: str, email: str, contact_id: str, subject: str) -> None:
    icon = STATUS_ICONS.get(status, "?")
    print(
        f"  {icon} [{status.upper():8s}]  {email:<40s}  id={contact_id}  │  {subject[:60]}"
    )


# ------------------------------------------------------------------
# Core sync logic
# ------------------------------------------------------------------

def process_messages(
    messages: list[dict],
    hubspot: HubSpotClient,
    state: StateStore,
) -> dict[str, int]:
    counts = {"created": 0, "updated": 0, "ignored": 0}

    for msg in messages:
        msg_id = msg["id"]

        if state.is_processed(msg_id):
            logger.debug("Skipping already-processed message %s", msg_id)
            counts["ignored"] += 1
            continue

        contact = extract_contact(msg["from"])
        if contact is None:
            logger.debug("No valid email in From header: %r", msg["from"])
            counts["ignored"] += 1
            continue

        # Skip noreply / automated senders
        local = contact.email.split("@")[0]
        if local in {"noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster"}:
            logger.debug("Skipping automated sender %s", contact.email)
            counts["ignored"] += 1
            state.mark_processed(msg_id, contact.email, "", "ignored")
            continue

        try:
            result = hubspot.sync_contact(
                contact=contact,
                message_id=msg_id,
                subject=msg.get("subject", ""),
                snippet=msg.get("snippet", ""),
                received_date=msg.get("date", ""),
            )
        except Exception as exc:
            logger.error("HubSpot sync failed for %s: %s", contact.email, exc)
            counts["ignored"] += 1
            continue

        state.mark_processed(msg_id, result.email, result.contact_id, result.status)
        counts[result.status] += 1
        _print_result(result.status, result.email, result.contact_id, msg.get("subject", ""))

    return counts


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process pending messages once and exit (default: run continuously)",
    )
    args = parser.parse_args()

    if not HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN is not set. Aborting.")
        sys.exit(1)

    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        logger.error(
            "Gmail credentials file not found: %s\n"
            "Download it from Google Cloud Console (OAuth 2.0 client ID).",
            GMAIL_CREDENTIALS_FILE,
        )
        sys.exit(1)

    state = StateStore(STATE_DB_PATH)
    gmail = GmailMonitor(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE, GMAIL_USER_EMAIL)
    hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)

    # Bootstrap history ID on first run
    history_id = state.get_history_id()
    if not history_id:
        history_id = gmail.get_current_history_id()
        state.save_history_id(history_id)
        logger.info("First run – bookmarked historyId=%s (future messages only)", history_id)

    # Graceful shutdown on SIGINT / SIGTERM
    _running = [True]

    def _stop(sig, frame):  # noqa: ANN001
        logger.info("Shutdown signal received – stopping after current poll")
        _running[0] = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(
        "Starting Gmail → HubSpot sync (poll every %ds, --once=%s)",
        POLL_INTERVAL,
        args.once,
    )
    print("=" * 72)
    print("  Gmail → HubSpot contact sync")
    print(f"  Poll interval : {POLL_INTERVAL}s")
    print(f"  State DB      : {STATE_DB_PATH}")
    print("=" * 72)

    while _running[0]:
        try:
            messages, new_history_id = gmail.poll_new_messages(history_id)

            if messages:
                print(f"\n[{_now()}]  {len(messages)} new message(s)")
                counts = process_messages(messages, hubspot, state)
                print(
                    f"  Summary → created={counts['created']}  "
                    f"updated={counts['updated']}  ignored={counts['ignored']}"
                )
            else:
                logger.debug("No new messages (historyId=%s)", new_history_id)

            if new_history_id != history_id:
                history_id = new_history_id
                state.save_history_id(history_id)

        except Exception as exc:
            logger.error("Unexpected error during poll cycle: %s", exc, exc_info=True)

        if args.once:
            break

        for _ in range(POLL_INTERVAL):
            if not _running[0]:
                break
            time.sleep(1)

    state.close()
    logger.info("Sync daemon stopped.")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


if __name__ == "__main__":
    main()
