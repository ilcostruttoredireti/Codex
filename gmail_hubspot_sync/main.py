#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Entry point — run once or continuously.

Usage:
    python -m gmail_hubspot_sync.main           # continuous loop
    python -m gmail_hubspot_sync.main --once    # single run
    python -m gmail_hubspot_sync.main --report  # print last results as JSON
"""

import argparse
import json
import logging
import sys

from .config import Config
from .sync import run_loop, run_once

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def _results_to_json(results) -> str:
    rows = []
    for r in results:
        rows.append(
            {
                "stato": r.status,
                "email": r.email,
                "hubspot_id": r.hubspot_contact_id,
                "dettagli": r.details or r.error or "",
            }
        )
    return json.dumps(rows, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run a single sync cycle and exit")
    parser.add_argument("--report", action="store_true", help="Run once and print JSON report")
    args = parser.parse_args()

    cfg = Config()

    if args.report:
        results = run_once(cfg)
        print(_results_to_json(results))
        sys.exit(0)

    if args.once:
        run_once(cfg)
        sys.exit(0)

    # Continuous loop
    for _ in run_loop(cfg):
        pass


if __name__ == "__main__":
    main()
