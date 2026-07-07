"""
Periodic scheduler — runs the Gmail→HubSpot sync on a configurable interval.

Usage:
    python scheduler.py              # runs every 15 minutes
    python scheduler.py --interval 5 # runs every 5 minutes
    python scheduler.py --once       # single run and exit
"""

from __future__ import annotations

import argparse
import logging
import time

from main import sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 15
DEFAULT_LOOKBACK_DAYS = 1


def run_once(days: int) -> None:
    log.info("Starting sync (lookback: %dd)…", days)
    results = sync(days=days)
    created = sum(1 for r in results if "Creato" in r["Stato"])
    updated = sum(1 for r in results if "Aggiornato" in r["Stato"])
    ignored = sum(1 for r in results if "Ignorato" in r["Stato"])
    log.info("Done — created=%d updated=%d ignored=%d", created, updated, ignored)


def main() -> None:
    parser = argparse.ArgumentParser(description="Periodic Gmail → HubSpot sync scheduler")
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_MINUTES,
        help=f"Sync interval in minutes (default: {DEFAULT_INTERVAL_MINUTES})",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=f"Gmail lookback window in days (default: {DEFAULT_LOOKBACK_DAYS})",
    )
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    if args.once:
        run_once(args.days)
        return

    log.info("Scheduler started — interval: %d min, lookback: %dd", args.interval, args.days)
    while True:
        try:
            run_once(args.days)
        except Exception as exc:
            log.error("Sync failed: %s", exc, exc_info=True)
        log.info("Next run in %d minutes…", args.interval)
        time.sleep(args.interval * 60)


if __name__ == "__main__":
    main()
