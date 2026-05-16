"""
Gmail → HubSpot contact sync.

Usage:
    python main.py [--once] [--interval SECONDS] [--no-timeline]

Env vars required:
    HUBSPOT_TOKEN               HubSpot private app token
    GMAIL_CREDENTIALS_FILE      Path to Google OAuth2 credentials JSON (default: credentials.json)

Optional env vars:
    GMAIL_TOKEN_FILE            Path to cached OAuth token (default: token.json)
    POLL_INTERVAL_SECONDS       Seconds between Gmail polls (default: 60)
    STATE_FILE                  Path to state JSON file (default: sync_state.json)
    IGNORED_DOMAINS             Comma-separated domains to skip (e.g. mycompany.com)
"""

import argparse
import logging
import sys
import time

from config import Config
from gmail_client import GmailClient, HistoryExpiredError
from hubspot_client import HubSpotClient
from state_manager import StateManager
from sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _print_result_table(results: list) -> None:
    if not results:
        return
    print("\n" + "─" * 72)
    print(f"{'STATUS':<10}{'EMAIL':<40}{'HUBSPOT ID'}")
    print("─" * 72)
    for r in results:
        print(f"{r.status.upper():<10}{r.email:<40}{r.contact_id}")
    print("─" * 72 + "\n")


def run(cfg: Config, once: bool, add_timeline: bool) -> None:
    cfg.validate()

    gmail = GmailClient(cfg.gmail_credentials_file, cfg.gmail_token_file)
    gmail.authenticate()

    hs = HubSpotClient(cfg.hubspot_token)
    state = StateManager(cfg.state_file)
    engine = SyncEngine(
        hubspot=hs,
        source=cfg.contact_source,
        tag=cfg.contact_tag,
        ignored_domains=cfg.ignored_domains,
        add_timeline=add_timeline,
    )

    # Initialise historyId on first run
    if not state.history_id:
        history_id = gmail.get_initial_history_id()
        state.history_id = history_id
        logger.info("First run — starting from historyId=%s", history_id)

    logger.info("Polling every %ds. Press Ctrl+C to stop.", cfg.poll_interval_seconds)

    while True:
        try:
            results = []
            try:
                for sender in gmail.poll_new_messages(state.history_id):
                    if state.is_processed(sender.message_id):
                        continue
                    result = engine.process(sender)
                    state.mark_processed(sender.message_id, {
                        "status": result.status,
                        "contact_id": result.contact_id,
                    })
                    results.append(result)
            except HistoryExpiredError:
                logger.warning("Gmail historyId expired — resetting to current position.")
                state.history_id = gmail.get_initial_history_id()
                continue

            # Advance state to latest historyId returned by Gmail
            if gmail.last_history_id:
                state.history_id = gmail.last_history_id

            if results:
                _print_result_table(results)
            else:
                logger.info("No new messages.")

        except KeyboardInterrupt:
            logger.info("Interrupted — goodbye.")
            sys.exit(0)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        if once:
            break
        time.sleep(cfg.poll_interval_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true",
                        help="Run a single poll then exit (useful for testing/cron)")
    parser.add_argument("--interval", type=int, default=None,
                        help="Override POLL_INTERVAL_SECONDS")
    parser.add_argument("--no-timeline", action="store_true",
                        help="Skip creating HubSpot timeline notes")
    args = parser.parse_args()

    cfg = Config()
    if args.interval is not None:
        cfg.poll_interval_seconds = args.interval

    run(cfg, once=args.once, add_timeline=not args.no_timeline)


if __name__ == "__main__":
    main()
