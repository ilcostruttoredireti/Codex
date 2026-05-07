#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Polls Gmail INBOX continuously and upserts senders as HubSpot contacts.

Usage:
    python main.py [--once] [--seed]

    --once   Process new emails once and exit (default: loop forever)
    --seed   Seed from recent INBOX messages instead of waiting for new ones
"""

import argparse
import logging
import sys
import time
from email.utils import parsedate_to_datetime
from datetime import timezone

from dotenv import load_dotenv

from config import Config
from gmail_hubspot_sync.contact_extractor import extract_sender_info
from gmail_hubspot_sync.gmail_monitor import GmailMonitor, HistoryExpiredError
from gmail_hubspot_sync.hubspot_sync import HubSpotSync, SyncStatus
from gmail_hubspot_sync.state_manager import StateManager


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _extract_email_metadata(message: dict) -> dict:
    """Pull Subject and Date from message headers."""
    headers = message.get("payload", {}).get("headers", [])
    meta = {}
    for h in headers:
        name = h.get("name", "").lower()
        if name == "subject":
            meta["subject"] = h.get("value", "")
        elif name == "date":
            meta["date"] = h.get("value", "")
    meta["message_id"] = message.get("id", "")
    return meta


def _print_result(status: SyncStatus, email: str, contact_id: str | None) -> None:
    status_icon = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–"}.get(status.value, "?")
    cid = contact_id or "N/A"
    print(f"  [{status_icon}] {status.value:10s}  {email:40s}  HubSpot ID: {cid}")


def process_message(
    message_id: str,
    gmail: GmailMonitor,
    hubspot: HubSpotSync,
    state: StateManager,
) -> bool:
    """Fetch, extract, sync one message. Returns True on success."""
    if state.is_processed(message_id):
        return False

    detail = gmail.get_message_detail(message_id)
    if not detail:
        return False

    contact_info = extract_sender_info(detail)
    if not contact_info:
        state.mark_processed(message_id)
        return False

    email_meta = _extract_email_metadata(detail)
    result = hubspot.sync_contact(contact_info, email_meta)

    _print_result(result.status, result.email, result.contact_id)
    state.mark_processed(message_id)
    return True


def seed_from_recent(gmail: GmailMonitor, hubspot: HubSpotSync, state: StateManager) -> None:
    """Process the most recent INBOX messages (one-time seed)."""
    print("Seeding from recent INBOX messages…")
    messages = gmail.get_recent_inbox_messages(max_results=100)
    count = 0
    for msg in messages:
        if process_message(msg["id"], gmail, hubspot, state):
            count += 1
    print(f"Seed complete – processed {count} messages.\n")


def run_loop(
    gmail: GmailMonitor,
    hubspot_sync: HubSpotSync,
    state: StateManager,
    poll_interval: int,
    run_once: bool,
) -> None:
    """Main polling loop."""
    # Ensure we have a starting history ID
    history_id = state.get_history_id()
    if not history_id:
        history_id = gmail.get_history_id()
        if not history_id:
            logging.critical("Cannot retrieve Gmail history ID. Exiting.")
            sys.exit(1)
        state.set_history_id(history_id)
        print(f"Starting from history ID {history_id}\n")

    print(f"Monitoring Gmail (polling every {poll_interval}s)…")
    print("-" * 70)

    while True:
        try:
            new_messages = list(gmail.get_new_messages(history_id))
            if new_messages:
                print(f"\n[+] {len(new_messages)} nuovi messaggi")
                for msg in new_messages:
                    process_message(msg["id"], gmail, hubspot_sync, state)

            # Advance history ID to current position
            new_history_id = gmail.get_history_id()
            if new_history_id and new_history_id != history_id:
                history_id = new_history_id
                state.set_history_id(history_id)

        except HistoryExpiredError:
            new_history_id = gmail.get_history_id()
            state.reset(new_history_id)
            history_id = new_history_id
            print("[!] History ID expired – stato resettato.")

        if run_once:
            break

        time.sleep(poll_interval)


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle then exit")
    parser.add_argument("--seed", action="store_true", help="Seed from recent INBOX messages")
    args = parser.parse_args()

    try:
        cfg = Config.from_env()
    except KeyError as e:
        print(f"Missing required environment variable: {e}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(cfg.log_level)
    logger = logging.getLogger(__name__)

    logger.debug("Initialising Gmail monitor…")
    gmail = GmailMonitor(
        credentials_path=cfg.gmail_credentials_file,
        token_path=cfg.gmail_token_file,
    )

    logger.debug("Initialising HubSpot sync…")
    hubspot = HubSpotSync(api_key=cfg.hubspot_access_token)

    state = StateManager(state_file=cfg.state_file)

    if args.seed:
        seed_from_recent(gmail, hubspot, state)

    run_loop(
        gmail=gmail,
        hubspot_sync=hubspot,
        state=state,
        poll_interval=cfg.poll_interval_seconds,
        run_once=args.once,
    )


if __name__ == "__main__":
    main()
