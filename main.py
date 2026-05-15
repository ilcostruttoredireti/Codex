#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync
=============================
Continuously polls the Gmail INBOX for new messages and upserts
each sender as a HubSpot contact.

Usage
-----
    cp .env.example .env          # fill in your credentials
    python main.py                # runs indefinitely (Ctrl-C to stop)
    python main.py --once         # single-pass (useful for cron / testing)

Output per email processed
--------------------------
    [STATUS] email@domain.com  (HubSpot ID: <id>)
    STATUS: Created / Updated / Ignored
"""

import argparse
import logging
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
import os

load_dotenv()

from gmail_monitor import GmailMonitor
from hubspot_sync import HubSpotSync

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── ANSI colours for terminal output ──────────────────────────────────────────
_C_GREEN = "\033[92m"
_C_BLUE = "\033[94m"
_C_YELLOW = "\033[93m"
_C_RESET = "\033[0m"

STATUS_COLOUR = {
    "created": _C_GREEN,
    "updated": _C_BLUE,
    "ignored": _C_YELLOW,
}


def _print_result(result: dict) -> None:
    status = result["status"]
    colour = STATUS_COLOUR.get(status, "")
    hs_id = result["contact_id"] or "—"
    print(
        f"  {colour}[{status.upper():8}]{_C_RESET}  "
        f"{result['email']:<45}  "
        f"HubSpot ID: {hs_id}"
    )


def run_once(monitor: GmailMonitor, syncer: HubSpotSync) -> int:
    """Process one batch of new messages. Returns count of non-ignored records."""
    messages = monitor.fetch_new_messages()
    if not messages:
        log.info("No new messages.")
        return 0

    log.info("Processing %d new message(s)…", len(messages))
    processed = 0
    for msg in messages:
        try:
            result = syncer.sync_contact(msg)
            _print_result(result)
            if result["status"] != "ignored":
                processed += 1
        except Exception as exc:
            log.error("Error syncing %s: %s", msg.get("email"), exc)
        finally:
            monitor.mark_processed(msg["message_id"])

    return processed


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single pass and exit (default: loop continuously)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL", "60")),
        help="Polling interval in seconds (default: 60)",
    )
    args = parser.parse_args()

    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    state_file = os.getenv("STATE_FILE", ".gmail_state.json")
    hubspot_key = os.getenv("HUBSPOT_API_KEY", "")

    if not hubspot_key:
        sys.exit("ERROR: HUBSPOT_API_KEY is not set. Check your .env file.")

    monitor = GmailMonitor(credentials_file, token_file, state_file)
    syncer = HubSpotSync(hubspot_key)

    print("=" * 70)
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Mode    : {'single pass' if args.once else 'continuous'}")
    if not args.once:
        print(f"  Interval: {args.interval}s")
    print("=" * 70)

    if args.once:
        run_once(monitor, syncer)
        return

    while True:
        print(f"\n── {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ────────────────")
        try:
            run_once(monitor, syncer)
        except KeyboardInterrupt:
            print("\nStopped.")
            break
        except Exception as exc:
            log.error("Unexpected error: %s", exc)

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nStopped.")
            break


if __name__ == "__main__":
    main()
