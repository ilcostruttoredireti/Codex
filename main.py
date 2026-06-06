#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync — continuous polling daemon.

Usage:
    python main.py             # run with defaults from .env
    python main.py --once      # single pass then exit
    python main.py --interval 30   # poll every 30 seconds
"""

import argparse
import logging
import os
import sys
import time
from dotenv import load_dotenv

from gmail_hubspot_sync.sync import SyncConfig, run_once

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _load_config() -> SyncConfig:
    required = {
        "GOOGLE_CLIENT_ID": os.getenv("GOOGLE_CLIENT_ID"),
        "GOOGLE_CLIENT_SECRET": os.getenv("GOOGLE_CLIENT_SECRET"),
        "GOOGLE_REFRESH_TOKEN": os.getenv("GOOGLE_REFRESH_TOKEN"),
        "HUBSPOT_ACCESS_TOKEN": os.getenv("HUBSPOT_ACCESS_TOKEN"),
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        logger.error("Missing required environment variables: %s", ", ".join(missing))
        logger.error("Copy .env.example to .env and fill in your credentials.")
        sys.exit(1)

    raw_skip = os.getenv("SKIP_DOMAINS", "gmail.com,googlemail.com,noreply.com,no-reply.com")
    skip_domains = {d.strip().lower() for d in raw_skip.split(",") if d.strip()}

    return SyncConfig(
        google_client_id=required["GOOGLE_CLIENT_ID"],
        google_client_secret=required["GOOGLE_CLIENT_SECRET"],
        google_refresh_token=required["GOOGLE_REFRESH_TOKEN"],
        hubspot_access_token=required["HUBSPOT_ACCESS_TOKEN"],
        skip_domains=skip_domains,
    )


def _print_results(results):
    if not results:
        logger.info("No new messages this pass.")
        return

    width = 72
    print("\n" + "─" * width)
    print(f"{'STATUS':<12} {'EMAIL':<38} {'HUBSPOT ID'}")
    print("─" * width)
    for r in results:
        cid = r.contact_id or r.detail or "—"
        print(f"{r.status:<12} {r.email:<38} {cid}")
    print("─" * width + "\n")

    created = sum(1 for r in results if r.status == "CREATED")
    updated = sum(1 for r in results if r.status == "UPDATED")
    skipped = sum(1 for r in results if r.status == "SKIPPED")
    errors  = sum(1 for r in results if r.status == "ERROR")
    logger.info("Pass complete — created=%d  updated=%d  skipped=%d  errors=%d",
                created, updated, skipped, errors)


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single pass then exit")
    parser.add_argument("--interval", type=int,
                        default=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
                        help="Polling interval in seconds (default: 60)")
    args = parser.parse_args()

    config = _load_config()

    logger.info("Gmail → HubSpot sync started (interval=%ds)", args.interval)
    logger.info("Skipping domains: %s", ", ".join(sorted(config.skip_domains)))

    if args.once:
        results = run_once(config)
        _print_results(results)
        return

    while True:
        try:
            results = run_once(config)
            _print_results(results)
        except KeyboardInterrupt:
            logger.info("Interrupted — shutting down.")
            break
        except Exception:
            logger.exception("Unhandled error in sync pass — will retry in %ds", args.interval)

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Interrupted during sleep — shutting down.")
            break


if __name__ == "__main__":
    main()
