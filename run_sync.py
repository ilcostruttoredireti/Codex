#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
-----------------------------
Continuously monitors the Gmail inbox and syncs new sender contacts to HubSpot.

Usage:
    python run_sync.py                # run once
    python run_sync.py --watch 60    # poll every 60 seconds
    python run_sync.py --since 1d    # process emails from the last day
"""

import argparse
import json
import logging
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from gmail_hubspot_sync.gmail_client import build_service, iter_new_messages
from gmail_hubspot_sync.hubspot_client import get_client
from gmail_hubspot_sync.sync import sync_messages, SyncStatus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

STATE_FILE = Path(".sync_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_processed_ts": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def parse_since(since_str: str) -> int:
    """Parse '1d', '2h', '30m' → Unix timestamp."""
    unit = since_str[-1]
    value = int(since_str[:-1])
    delta_map = {"d": timedelta(days=value), "h": timedelta(hours=value), "m": timedelta(minutes=value)}
    if unit not in delta_map:
        raise ValueError(f"Invalid --since format: {since_str!r}. Use e.g. '1d', '6h', '30m'")
    return int((datetime.now(timezone.utc) - delta_map[unit]).timestamp())


def run_once(gmail_service, hs_client, after_ts: int | None) -> int:
    """Fetch new messages, sync contacts, return latest timestamp seen."""
    log.info("Fetching inbox messages%s …", f" since {after_ts}" if after_ts else "")
    messages = list(iter_new_messages(gmail_service, after_timestamp=after_ts))
    log.info("Found %d message(s) to process", len(messages))

    if not messages:
        return after_ts or 0

    results = sync_messages(messages, hs_client)

    created = [r for r in results if r.status == SyncStatus.CREATED]
    updated = [r for r in results if r.status == SyncStatus.UPDATED]
    ignored = [r for r in results if r.status == SyncStatus.IGNORED]

    print("\n" + "─" * 60)
    print(f"  Processati: {len(results)}  |  Creati: {len(created)}  |  Aggiornati: {len(updated)}  |  Ignorati: {len(ignored)}")
    print("─" * 60)
    for r in results:
        print(f"  {r}")
    print("─" * 60 + "\n")

    return int(datetime.now(timezone.utc).timestamp())


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Poll interval in seconds (omit for one-shot run)")
    parser.add_argument("--since", metavar="DURATION", help="Process emails since duration ago (e.g. 1d, 6h, 30m)")
    parser.add_argument("--reset", action="store_true", help="Clear saved state and start fresh")
    args = parser.parse_args()

    if args.reset and STATE_FILE.exists():
        STATE_FILE.unlink()
        log.info("State reset.")

    state = load_state()

    after_ts: int | None = None
    if args.since:
        after_ts = parse_since(args.since)
    elif state["last_processed_ts"]:
        after_ts = state["last_processed_ts"]

    gmail_service = build_service()
    hs_client = get_client()

    def _shutdown(sig, frame):
        log.info("Shutting down …")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.watch:
        log.info("Watch mode: polling every %ds", args.watch)
        while True:
            new_ts = run_once(gmail_service, hs_client, after_ts)
            state["last_processed_ts"] = new_ts
            save_state(state)
            after_ts = new_ts
            time.sleep(args.watch)
    else:
        new_ts = run_once(gmail_service, hs_client, after_ts)
        state["last_processed_ts"] = new_ts
        save_state(state)


if __name__ == "__main__":
    main()
