"""
main.py – entry point: authenticates, then runs the sync loop

Usage:
    python main.py                 # run once
    python main.py --loop          # run continuously (uses POLL_INTERVAL_SECONDS)
    python main.py --loop --no-activity  # disable timeline activity creation
"""
from __future__ import annotations

import argparse
import signal
import sys
import time

import schedule

from config import config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from logger import get_logger
from sync import GmailHubSpotSyncer

logger = get_logger(log_level=config.LOG_LEVEL, log_file=config.LOG_FILE)

_BANNER = r"""
 ██████╗ ███╗   ███╗ █████╗ ██╗██╗      ██╗  ██╗██╗   ██╗██████╗ ███████╗██████╗  ██████╗ ████████╗
██╔════╝ ████╗ ████║██╔══██╗██║██║      ██║  ██║██║   ██║██╔══██╗██╔════╝██╔══██╗██╔═══██╗╚══██╔══╝
██║  ███╗██╔████╔██║███████║██║██║      ███████║██║   ██║██████╔╝███████╗██████╔╝██║   ██║   ██║
██║   ██║██║╚██╔╝██║██╔══██║██║██║      ██╔══██║██║   ██║██╔══██╗╚════██║██╔═══╝ ██║   ██║   ██║
╚██████╔╝██║ ╚═╝ ██║██║  ██║██║███████╗ ██║  ██║╚██████╔╝██████╔╝███████║██║     ╚██████╔╝   ██║
 ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝╚══════╝ ╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚══════╝╚═╝      ╚═════╝    ╚═╝
          Gmail → HubSpot Contact Sync
"""

_running = True


def _handle_sigterm(sig, frame):  # noqa: ANN001
    global _running
    logger.info("Received SIGTERM – shutting down gracefully…")
    _running = False


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--loop",
        action="store_true",
        help=f"Run continuously every {config.POLL_INTERVAL_SECONDS}s",
    )
    parser.add_argument(
        "--no-activity",
        dest="no_activity",
        action="store_true",
        help="Skip creating HubSpot timeline activities",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=config.POLL_INTERVAL_SECONDS,
        help="Override polling interval in seconds (only with --loop)",
    )
    args = parser.parse_args()

    print(_BANNER)

    # ── Validate config ───────────────────────────────────────
    try:
        config.validate()
    except ValueError as exc:
        logger.critical("Configurazione non valida:\n%s", exc)
        sys.exit(1)

    # ── Authenticate ──────────────────────────────────────────
    gmail = GmailClient()
    gmail.authenticate()

    hubspot_client = HubSpotClient()
    syncer = GmailHubSpotSyncer(gmail=gmail, hubspot=hubspot_client)

    create_activities = not args.no_activity

    def _sync_job() -> None:
        logger.info("▶ Avvio sincronizzazione…")
        try:
            syncer.run_once(create_activities=create_activities)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Errore non gestito durante la sincronizzazione: %s", exc)

    # ── Run mode ──────────────────────────────────────────────
    if args.loop:
        interval = args.interval
        logger.info(
            "Modalità loop attiva – sincronizzazione ogni %ds  (Ctrl+C per uscire)",
            interval,
        )
        schedule.every(interval).seconds.do(_sync_job)

        # Run immediately on start
        _sync_job()

        while _running:
            schedule.run_pending()
            time.sleep(1)

        logger.info("Sync loop terminato.")
    else:
        _sync_job()


if __name__ == "__main__":
    main()
