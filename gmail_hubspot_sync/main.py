"""Entry point — run once or loop continuously."""
import logging
import time
import argparse

from . import config
from .sync_engine import print_report, run_sync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single sync pass and exit")
    parser.add_argument("--days", type=int, default=config.DAYS_LOOKBACK,
                        help="How many days back to scan (default: DAYS_LOOKBACK env)")
    parser.add_argument("--interval", type=int, default=config.POLL_INTERVAL_SECONDS,
                        help="Polling interval in seconds (default: POLL_INTERVAL_SECONDS env)")
    args = parser.parse_args()

    if args.once:
        logger.info("Running single sync pass (last %d days)…", args.days)
        results = run_sync(days_back=args.days)
        print_report(results)
        return

    logger.info("Starting continuous sync — interval %ds, lookback %dd", args.interval, args.days)
    while True:
        try:
            logger.info("Sync pass started")
            results = run_sync(days_back=args.days)
            print_report(results)
        except Exception as exc:
            logger.error("Sync pass failed: %s", exc)
        logger.info("Sleeping %ds…", args.interval)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
