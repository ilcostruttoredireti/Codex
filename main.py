#!/usr/bin/env python3
"""Entry point: Gmail → HubSpot contact sync.

Usage:
    python main.py              # run forever (polling loop)
    python main.py --once       # single pass then exit
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"ERROR: environment variable {name!r} is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit.")
    args = parser.parse_args()

    from src.gmail_to_hubspot.gmail_client import GmailClient
    from src.gmail_to_hubspot.hubspot_client import HubSpotClient
    from src.gmail_to_hubspot.sync import GmailHubSpotSync

    gmail = GmailClient(
        credentials_file=os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        token_file=os.environ.get("GMAIL_TOKEN_FILE", "token.json"),
    )

    hubspot = HubSpotClient(
        access_token=_require_env("HUBSPOT_ACCESS_TOKEN"),
    )

    sync = GmailHubSpotSync(
        gmail=gmail,
        hubspot=hubspot,
        state_file=os.environ.get("STATE_FILE", ".gmail_sync_state"),
        poll_interval=int(os.environ.get("POLL_INTERVAL", "60")),
    )

    if args.once:
        results = sync.run_once()
        print(f"\nRisultato: {len(results)} email processate.")
        for r in results:
            print(f"  [{r.status.value:10s}]  {r.email:40s}  ID={r.contact_id}")
    else:
        sync.run_forever()


if __name__ == "__main__":
    main()
