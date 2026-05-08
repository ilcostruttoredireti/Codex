"""
Gmail → HubSpot contact sync daemon.

Usage:
    python main.py [--once]

Options:
    --once   Process the current inbox once and exit (no polling loop).

Environment variables (see .env.example):
    HUBSPOT_API_KEY           required
    GMAIL_CREDENTIALS_FILE    default: credentials.json
    GMAIL_TOKEN_FILE          default: token.json
    POLL_INTERVAL_SECONDS     default: 60
    IGNORED_DOMAINS           comma-separated domains to skip
"""
import argparse
import json
import logging
import signal
import sys
import time

from dotenv import load_dotenv

load_dotenv()  # loads .env if present; no-op when the file is absent

from config import Config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync import Syncer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_SHUTDOWN = False


def _handle_sigterm(signum, frame):  # noqa: ANN001
    global _SHUTDOWN
    _SHUTDOWN = True
    logger.info("Shutdown signal received — finishing current cycle…")


def _run_once(syncer: Syncer) -> list[dict]:
    results = syncer.run_once()
    return [
        {
            "status": r.status,
            "email": r.contact_email,
            "hubspot_id": r.contact_id,
        }
        for r in results
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single sync cycle and print JSON results, then exit",
    )
    args = parser.parse_args()

    cfg = Config()
    try:
        cfg.validate()
    except (ValueError, FileNotFoundError) as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    gmail = GmailClient(cfg.gmail_credentials_file, cfg.gmail_token_file)
    gmail.connect()

    hubspot = HubSpotClient(cfg.hubspot_api_key)
    syncer = Syncer(gmail, hubspot, ignored_domains=cfg.ignored_domains)

    if args.once:
        results = _run_once(syncer)
        print(json.dumps(results, indent=2, ensure_ascii=False))
        return

    # Continuous polling loop
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)

    logger.info(
        "Starting Gmail → HubSpot sync (poll interval: %ds)", cfg.poll_interval_seconds
    )

    while not _SHUTDOWN:
        try:
            results = _run_once(syncer)
            processed = len([r for r in results if r["status"] != "ignored"])
            if processed:
                logger.info("Cycle complete: %d contact(s) processed", processed)
            else:
                logger.debug("Cycle complete: no new contacts")
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error during sync cycle: %s", exc, exc_info=True)

        # Sleep in 1-second ticks so SIGINT/SIGTERM is responsive
        for _ in range(cfg.poll_interval_seconds):
            if _SHUTDOWN:
                break
            time.sleep(1)

    logger.info("Sync daemon stopped.")


if __name__ == "__main__":
    main()
