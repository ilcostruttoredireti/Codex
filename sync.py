#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync.

Polls the inbox for new emails, extracts sender data, and creates or
updates the matching contact in HubSpot — skipping duplicates and
no-reply addresses automatically.
"""

import logging
import sys
import time
from datetime import datetime

from config import Config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from processor import ContactProcessor
from state_manager import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_ICONS = {"created": "+", "updated": "~", "skipped": "-", "error": "!"}
_LINE = "─" * 72


def _print_result(r: dict) -> None:
    icon = _ICONS.get(r["status"], "?")
    cid = r.get("contact_id") or "N/A"
    print(f"  [{icon}] {r['status'].upper():<9} {r['email']:<42} HubSpot ID: {cid}")


def _report(results: list) -> None:
    if not results:
        return
    print(f"\n{_LINE}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  —  {len(results)} message(s) processed")
    print(_LINE)
    for r in results:
        _print_result(r)
    counts: dict = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(_LINE)
    print("  " + "  |  ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    print(f"{_LINE}\n")


def _build_components(config: Config):
    gmail = GmailClient(config)
    hubspot = HubSpotClient(config)
    state = StateManager(config.state_file)
    processor = ContactProcessor(gmail, hubspot, state)
    return processor


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Sync Gmail sender contacts to HubSpot CRM",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python sync.py                     continuous polling (60s default)
  python sync.py --interval 300      poll every 5 minutes
  python sync.py --once              single pass, then exit
  python sync.py --backfill 7        import senders from last 7 days, then exit
""",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        metavar="SEC",
        help="Polling interval in seconds (overrides POLL_INTERVAL env var)",
    )
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="DAYS",
        help="Backfill contacts from the last N days, then exit",
    )
    args = parser.parse_args()

    try:
        config = Config()
    except (ValueError, FileNotFoundError) as exc:
        logger.error(str(exc))
        sys.exit(1)

    interval = args.interval if args.interval is not None else config.poll_interval

    try:
        processor = _build_components(config)
    except Exception as exc:
        logger.error("Initialization failed: %s", exc)
        sys.exit(1)

    if args.backfill > 0:
        logger.info("Backfill mode: last %d day(s)", args.backfill)
        _report(processor.sync_once(backfill_days=args.backfill))
        return

    if args.once:
        logger.info("Single sync cycle")
        _report(processor.sync_once())
        return

    logger.info("Continuous sync started (interval=%ds). Press Ctrl+C to stop.", interval)
    while True:
        try:
            _report(processor.sync_once())
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            break
        except Exception as exc:
            logger.exception("Sync cycle error: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
