#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Usage:
  python main.py              # continuous polling (default 60s interval)
  python main.py --once       # single pass then exit
  python main.py --interval 120  # custom poll interval in seconds
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.sync_engine import SyncEngine


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        print(f"ERROR: environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts.")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass then exit.")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default: 60).")
    args = parser.parse_args()

    hubspot_token = _require_env("HUBSPOT_TOKEN")
    credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    state_file = os.environ.get("GMAIL_STATE_FILE", "gmail_state.json")

    gmail = GmailClient(
        credentials_file=credentials_file,
        token_file=token_file,
        state_file=state_file,
    )
    hubspot = HubSpotClient(token=hubspot_token)
    engine = SyncEngine(gmail=gmail, hubspot=hubspot, poll_interval=args.interval)

    if args.once:
        results = engine.run_once()
        print(f"\nDone. Processed {len(results)} message(s).")
        for r in results:
            print(f"  {r['status'].upper():10s} {r['email']:40s} {r.get('id', '')}")
    else:
        engine.run_forever()


if __name__ == "__main__":
    main()
