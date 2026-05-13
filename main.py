"""
Gmail → HubSpot contact sync — entry point.

Usage:
    python main.py [--once]

Flags:
    --once   Run a single polling cycle then exit (useful for cron/testing).
             Default: run continuously.
"""

import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from gmail_monitor import GmailMonitor
from hubspot_sync import HubSpotSync, SyncStatus

load_dotenv()


def _setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"[ERROR] Environment variable '{name}' is required. "
              f"Copy .env.example → .env and fill in the values.", file=sys.stderr)
        sys.exit(1)
    return value


def _print_result(result) -> None:
    icon = {
        SyncStatus.CREATED: "✅",
        SyncStatus.UPDATED: "🔄",
        SyncStatus.IGNORED: "⏭️ ",
    }.get(result.status, "❓")

    detail = f"  ({result.detail})" if result.detail else ""
    print(
        f"{icon}  [{result.status.value:10s}]  "
        f"{result.contact_email:<40s}  "
        f"ID: {result.hubspot_contact_id or 'n/a'}"
        f"{detail}"
    )


def run(once: bool = False) -> None:
    _setup_logging()
    logger = logging.getLogger(__name__)

    # Config
    credentials_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")
    gmail_query = os.getenv("GMAIL_QUERY", "label:inbox is:unread")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    owner_id = os.getenv("HUBSPOT_OWNER_ID") or None

    # Modules
    monitor = GmailMonitor(
        credentials_file=credentials_file,
        token_file=token_file,
        query=gmail_query,
        poll_interval=poll_interval,
    )
    syncer = HubSpotSync(access_token=hubspot_token, owner_id=owner_id)

    print("=" * 70)
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Query  : {gmail_query}")
    print(f"  Mode   : {'single run' if once else 'continuous'}")
    print("=" * 70)

    stats = {SyncStatus.CREATED: 0, SyncStatus.UPDATED: 0, SyncStatus.IGNORED: 0}

    try:
        generator = monitor.poll_once() if once else monitor.run_forever()
        for sender in generator:
            logger.debug("Processing sender: %s <%s>", sender.full_name, sender.email)
            result = syncer.sync(sender)
            stats[result.status] += 1
            _print_result(result)

    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente.")
    finally:
        print("\n--- Riepilogo ---")
        print(f"  Creati  : {stats[SyncStatus.CREATED]}")
        print(f"  Aggiornati: {stats[SyncStatus.UPDATED]}")
        print(f"  Ignorati: {stats[SyncStatus.IGNORED]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single polling cycle then exit.",
    )
    args = parser.parse_args()
    run(once=args.once)


if __name__ == "__main__":
    main()
