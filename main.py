#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Usage:
    python main.py                  # continuous polling daemon
    python main.py --once           # single poll cycle, then exit
    python main.py --initial-sync   # process existing INBOX on first run
"""
import argparse
import logging
import sys

from dotenv import load_dotenv

from gmail_hubspot_sync.config import Config
from gmail_hubspot_sync.sync import GmailHubSpotSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync inbound Gmail senders to HubSpot")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single poll cycle and exit",
    )
    parser.add_argument(
        "--initial-sync",
        action="store_true",
        help="On the very first run, import all recent INBOX messages (up to 200)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to .env file (default: .env)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    load_dotenv(args.env_file)

    try:
        config = Config.from_env()
    except KeyError as exc:
        print(f"Variabile d'ambiente mancante: {exc}", file=sys.stderr)
        sys.exit(1)

    sync = GmailHubSpotSync(config)

    if args.once or args.initial_sync:
        report = sync.run_once(initial_sync=args.initial_sync)
        report.print_summary()
    else:
        sync.run_forever()


if __name__ == "__main__":
    main()
