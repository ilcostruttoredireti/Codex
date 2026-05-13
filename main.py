#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Usage:
    python main.py [--once] [--interval SECONDS] [--max-results N]
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync import GmailHubSpotSync


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts")
    parser.add_argument("--once", action="store_true", help="Run a single pass then exit")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds (default 60)")
    parser.add_argument("--max-results", type=int, default=50, help="Max emails per poll cycle (default 50)")
    parser.add_argument("--credentials", default="credentials.json", help="Path to Gmail OAuth credentials file")
    parser.add_argument("--token", default="token.pickle", help="Path to stored OAuth token")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = _parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    hubspot_key = os.getenv("HUBSPOT_API_KEY")
    if not hubspot_key:
        print("ERROR: HUBSPOT_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.credentials):
        print(f"ERROR: Gmail credentials file not found: {args.credentials}", file=sys.stderr)
        print("Download it from Google Cloud Console → APIs & Services → Credentials", file=sys.stderr)
        sys.exit(1)

    gmail = GmailClient(credentials_file=args.credentials, token_file=args.token)
    gmail.connect()

    hubspot = HubSpotClient(api_key=hubspot_key)

    syncer = GmailHubSpotSync(gmail=gmail, hubspot=hubspot, poll_interval=args.interval)

    if args.once:
        results = syncer.run_once()
        created = sum(1 for r in results if r.status.value == "Creato")
        updated = sum(1 for r in results if r.status.value == "Aggiornato")
        ignored = sum(1 for r in results if r.status.value in ("Ignorato", "Errore"))
        print(f"\nSummary: {created} created, {updated} updated, {ignored} ignored/error")
    else:
        syncer.run_forever()


if __name__ == "__main__":
    main()
