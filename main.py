#!/usr/bin/env python3
"""
Entry point for the Gmail → HubSpot contact sync.

Usage:
  python main.py           # continuous polling loop
  python main.py --once    # single poll cycle and exit
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from gmail_hubspot_sync.sync import run_loop, run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    args = parser.parse_args()

    if args.once:
        results = run_once()
        _print_summary(results)
    else:
        run_loop()


def _print_summary(results: list) -> None:
    if not results:
        print("Nessun contatto processato in questo ciclo.")
        return
    print(f"\n{'─'*70}")
    print(f"{'STATO':<12} {'EMAIL':<40} {'HUBSPOT ID'}")
    print(f"{'─'*70}")
    for r in results:
        print(f"{r.status.upper():<12} {r.email:<40} {r.contact_id or 'N/A'}")
    print(f"{'─'*70}")
    created = sum(1 for r in results if r.status == "created")
    updated = sum(1 for r in results if r.status == "updated")
    skipped = sum(1 for r in results if r.status == "skipped")
    print(f"Totale: {len(results)} | Creati: {created} | Aggiornati: {updated} | Ignorati: {skipped}\n")


if __name__ == "__main__":
    main()
