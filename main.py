#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
─────────────────────────────
Monitors Gmail inbox and syncs sender contacts into HubSpot CRM.

Usage:
  python main.py            # daemon mode (polls every POLL_INTERVAL_SECONDS)
  python main.py --once     # single pass then exit
  python main.py --interval 120   # daemon with custom poll interval (seconds)
"""

import argparse
import logging
import signal
import sys
import time

from gmail_hubspot_sync.config import Config
from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.state import SyncState
from gmail_hubspot_sync.sync import SyncEngine


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ── Signal handler for clean shutdown ─────────────────────────────────────────

_stop = False


def _handle_signal(sig, frame):  # noqa: ANN001
    global _stop
    log.info("Shutdown signal received – finishing current pass then exiting.")
    _stop = True


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single sync pass and exit (default: daemon loop)"
    )
    parser.add_argument(
        "--interval", type=int, default=None,
        help="Poll interval in seconds (overrides POLL_INTERVAL_SECONDS env var)"
    )
    parser.add_argument(
        "--debug", action="store_true", help="Enable DEBUG logging"
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    try:
        Config.validate()
    except EnvironmentError as exc:
        log.error("%s", exc)
        sys.exit(1)

    interval = args.interval or Config.POLL_INTERVAL

    log.info("─" * 60)
    log.info("Gmail → HubSpot sync starting")
    log.info("Mode     : %s", "single pass" if args.once else f"daemon (every {interval}s)")
    log.info("State    : %s", Config.STATE_FILE)
    log.info("─" * 60)

    # Initialise clients (Gmail triggers OAuth browser flow on first run)
    gmail = GmailClient()
    hubspot = HubSpotClient()
    state = SyncState(Config.STATE_FILE)

    engine = SyncEngine(gmail=gmail, hubspot=hubspot, state=state)

    if args.once:
        _run_pass(engine)
        return

    # Daemon loop
    while not _stop:
        _run_pass(engine)
        log.info("Next run in %ds. Press Ctrl+C to stop.", interval)
        for _ in range(interval):
            if _stop:
                break
            time.sleep(1)

    log.info("Sync daemon stopped.")


def _run_pass(engine: SyncEngine) -> None:
    log.info("── Starting sync pass ──────────────────────────────────────────")
    results = engine.run_once()
    totals = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}
    for r in results:
        totals[r.outcome.value] = totals.get(r.outcome.value, 0) + 1
    log.info(
        "── Pass complete: %d created | %d updated | %d skipped | %d errors ──",
        totals["Creato"], totals["Aggiornato"], totals["Ignorato"], totals["Errore"],
    )


if __name__ == "__main__":
    main()
