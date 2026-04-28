"""
Gmail → HubSpot contact sync
=============================
Continuously polls Gmail for new inbound messages, extracts sender data,
and creates/updates contacts in HubSpot.

Usage:
    python main.py [--interval SECONDS] [--once]

Options:
    --interval   Poll interval in seconds (default: 60)
    --once       Process new messages once then exit (useful for cron)
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone

from config import load_config
from contact_processor import extract_contact
from gmail_client import GmailClient
from hubspot_client import HubSpotClient, SyncResult

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("gmail_hubspot_sync")


def _print_result(result: SyncResult, subject: str) -> None:
    status_icon = {"created": "✚", "updated": "✎", "ignored": "—"}.get(result.status, "?")
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(
        f"[{ts}] {status_icon} {result.status.upper():8s} | "
        f"email={result.email} | "
        f"id={result.contact_id} | "
        f"subject={subject[:60]!r}"
    )
    if result.reason:
        print(f"          reason: {result.reason}")


def process_once(gmail: GmailClient, hubspot: HubSpotClient) -> int:
    """Fetch new messages and sync contacts. Returns number of messages processed."""
    processed = 0

    for message in gmail.new_messages():
        processed += 1
        from_email = message.get("from_email", "<unknown>")
        subject = message.get("subject", "")

        contact = extract_contact(message)
        if contact is None:
            logger.info("Skipped automated sender: %s", from_email)
            print(
                f"         — SKIPPED  | email={from_email} | subject={subject[:60]!r} "
                f"(automated/noreply)"
            )
            continue

        try:
            result = hubspot.upsert_contact(contact)
        except Exception as exc:
            logger.error("HubSpot error for %s: %s", from_email, exc)
            print(f"         ✗ ERROR    | email={from_email} | {exc}")
            continue

        _print_result(result, subject)

        # Optional: add timeline event (requires HUBSPOT_APP_ID in config)
        if result.status in ("created", "updated"):
            try:
                hubspot.add_timeline_event(
                    contact_id=result.contact_id,
                    email_subject=subject,
                    email_snippet=message.get("snippet", ""),
                    message_id=message["id"],
                )
            except Exception as exc:
                logger.warning("Timeline event failed: %s", exc)

    return processed


def run(interval: int, once: bool) -> None:
    config = load_config()

    gmail = GmailClient(
        credentials_path=config.gmail_credentials_path,
        token_path=config.gmail_token_path,
    )
    hubspot = HubSpotClient(
        access_token=config.hubspot_access_token,
        app_id=config.hubspot_app_id,
    )

    logger.info("Gmail → HubSpot sync started (interval=%ds)", interval)
    print("=" * 70)
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Poll interval : {interval}s")
    print(f"  Mode          : {'single run' if once else 'continuous'}")
    print("=" * 70)

    while True:
        try:
            count = process_once(gmail, hubspot)
            if count:
                logger.info("Processed %d message(s)", count)
            else:
                logger.debug("No new messages")
        except KeyboardInterrupt:
            logger.info("Interrupted by user — exiting")
            sys.exit(0)
        except Exception as exc:
            logger.error("Unexpected error: %s", exc, exc_info=True)

        if once:
            break

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            logger.info("Interrupted by user — exiting")
            sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Gmail senders to HubSpot contacts")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SECONDS",
        help="Poll interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process once then exit (for use with cron)",
    )
    args = parser.parse_args()
    run(interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
