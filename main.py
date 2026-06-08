#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Monitors the Gmail inbox and syncs every new sender as a HubSpot contact.

Usage:
  python main.py               # continuous mode (polls every POLL_INTERVAL seconds)
  python main.py --once        # single pass then exit
  python main.py --dry-run     # simulate without writing to HubSpot
  python main.py --interval 30 # override poll interval
"""
import argparse
import logging
import sys
import time

from sync.config import Config
from sync.sync_engine import SyncEngine


def _setup_logging(level: str) -> None:
    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("gmail_hubspot_sync.log"),
        ],
    )


def _print_result_table(results) -> None:
    if not results:
        print("  (nessuna nuova email)")
        return
    print(f"\n{'STATO':<12} {'EMAIL':<40} {'HUBSPOT ID'}")
    print("-" * 70)
    for r in results:
        hid = r.hubspot_id or ("-" if r.status == "Ignorato" else "?")
        print(f"{r.status:<12} {r.email:<40} {hid}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate sync without writing to HubSpot")
    parser.add_argument("--interval", type=int, default=None,
                        help="Override POLL_INTERVAL (seconds)")
    args = parser.parse_args()

    config = Config()
    _setup_logging(config.LOG_LEVEL)
    logger = logging.getLogger(__name__)

    try:
        config.validate()
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        sys.exit(1)

    if args.dry_run:
        logger.info("DRY RUN mode — no changes will be made in HubSpot")

    engine = SyncEngine(config, dry_run=args.dry_run)
    interval = args.interval or config.POLL_INTERVAL

    if args.once:
        results = engine.run_once()
        _print_result_table(results)
        return

    logger.info(f"Starting continuous sync (interval: {interval}s). Ctrl+C to stop.")
    while True:
        try:
            results = engine.run_once()
            _print_result_table(results)
        except KeyboardInterrupt:
            logger.info("Interrupted by user — exiting.")
            break
        except Exception as e:
            logger.error(f"Unexpected error during sync: {e}", exc_info=True)

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Interrupted by user — exiting.")
            break


if __name__ == "__main__":
    main()
