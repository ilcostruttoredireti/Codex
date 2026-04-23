#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Polls the Gmail inbox for new messages, extracts sender information,
and creates or updates contacts in HubSpot — deduplicating by email address.

Usage:
    python sync.py                 # run once
    python sync.py --loop          # continuous polling (uses POLL_INTERVAL)
    python sync.py --init          # bootstrap: set the starting history ID and exit
"""

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient, SyncStatus

# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("sync")


# ---------------------------------------------------------------------------
# State persistence (stores last Gmail historyId)
# ---------------------------------------------------------------------------

def _state_path() -> Path:
    return Path(os.getenv("STATE_FILE", ".gmail_state.json"))


def load_state() -> dict:
    p = _state_path()
    if p.exists():
        with p.open() as fh:
            return json.load(fh)
    return {}


def save_state(state: dict) -> None:
    with _state_path().open("w") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Single sync pass
# ---------------------------------------------------------------------------

def run_once(gmail: GmailClient, hubspot: HubSpotClient) -> list[dict]:
    """
    Process all new inbox messages since the last saved historyId.
    Returns a list of result dicts for logging / downstream use.
    """
    state = load_state()
    history_id = state.get("last_history_id")

    if not history_id:
        # First run: nothing to diff yet — save current ID and exit.
        history_id = gmail.get_current_history_id()
        save_state({"last_history_id": history_id})
        logger.info(
            "First run: saved historyId %s. "
            "New emails arriving after this point will be synced on the next run.",
            history_id,
        )
        return []

    senders, new_history_id = gmail.fetch_new_senders(history_id)
    save_state({"last_history_id": new_history_id})

    if not senders:
        logger.info("No new inbox messages since historyId %s.", history_id)
        return []

    logger.info("Processing %d new message(s).", len(senders))

    results = []
    seen_emails: set[str] = set()   # deduplicate within this batch

    for sender in senders:
        if sender.email in seen_emails:
            logger.debug("Skipping duplicate sender %s in this batch.", sender.email)
            continue
        seen_emails.add(sender.email)

        result = hubspot.sync_sender(
            email=sender.email,
            first_name=sender.first_name,
            last_name=sender.last_name,
            company=sender.company,
            subject=sender.subject,
            received_at=sender.received_at,
        )

        status_label = result.status.name  # CREATED / UPDATED / IGNORED
        icon = {"CREATED": "✚", "UPDATED": "↻", "IGNORED": "—"}.get(status_label, "?")

        logger.info(
            "%s %-8s  %-40s  HubSpot ID: %s  %s",
            icon,
            status_label,
            result.contact_email,
            result.contact_id or "N/A",
            result.detail,
        )
        results.append(
            {
                "status": status_label,
                "email": result.contact_email,
                "hubspot_id": result.contact_id,
                "detail": result.detail,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_clients() -> tuple[GmailClient, HubSpotClient]:
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    hubspot_key = os.getenv("HUBSPOT_API_KEY", "")
    timeline_event_type_id = os.getenv("HUBSPOT_TIMELINE_EVENT_TYPE_ID")

    if not hubspot_key:
        logger.error("HUBSPOT_API_KEY is not set. Aborting.")
        sys.exit(1)

    gmail = GmailClient(credentials_file, token_file)
    gmail.authenticate()

    hs = HubSpotClient(hubspot_key, timeline_event_type_id)
    return gmail, hs


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Run continuously (polling every POLL_INTERVAL seconds)",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="Save the current Gmail historyId as the starting point and exit",
    )
    args = parser.parse_args()

    gmail, hs = build_clients()

    if args.init:
        history_id = gmail.get_current_history_id()
        save_state({"last_history_id": history_id})
        logger.info("Initialised. Starting historyId: %s", history_id)
        return

    if args.loop:
        interval = int(os.getenv("POLL_INTERVAL", "60"))
        logger.info("Starting continuous sync (interval: %ds). Press Ctrl+C to stop.", interval)
        try:
            while True:
                run_once(gmail, hs)
                time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
    else:
        run_once(gmail, hs)


if __name__ == "__main__":
    main()
