"""
Gmail → HubSpot contact sync.

Usage:
    python main.py [--once]

    --once    Process current inbox once and exit (no loop).
"""

import os
import sys
import time
import argparse
from dotenv import load_dotenv

from gmail_client import build_gmail_service, fetch_messages, parse_sender
from hubspot_client import HubSpotClient
from state_store import init_db, is_processed, mark_processed
from sync import sync_sender

load_dotenv()


def _print_result(result: dict, index: int) -> None:
    icon = {"created": "✚", "updated": "↻", "ignored": "—"}.get(result["status"], "?")
    print(
        f"  [{index:>4}] {icon} {result['status'].upper():<8}"
        f"  {result['email']:<40}"
        f"  HubSpot ID: {result.get('contact_id', 'n/a')}"
    )


def run_once(gmail_service, hs_client: HubSpotClient, db, query: str) -> int:
    messages = fetch_messages(gmail_service, query)
    print(f"  Found {len(messages)} message(s) matching query.")

    processed_count = 0
    for idx, stub in enumerate(messages, start=1):
        msg_id = stub["id"]

        if is_processed(db, msg_id):
            continue

        sender = parse_sender(gmail_service, msg_id)
        mark_processed(db, msg_id)

        if sender is None:
            print(f"  [{idx:>4}] — SKIPPED   (no valid sender)")
            continue

        try:
            result = sync_sender(hs_client, sender, log_timeline=True)
            _print_result(result, idx)
            processed_count += 1
        except Exception as exc:
            print(f"  [{idx:>4}] ! ERROR     {sender['email']} — {exc}")

    return processed_count


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args()

    creds_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    hs_key = os.getenv("HUBSPOT_API_KEY")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    query = os.getenv("GMAIL_QUERY", "in:inbox")

    if not hs_key:
        sys.exit("ERROR: HUBSPOT_API_KEY is not set. Copy .env.example → .env and fill it in.")

    print("Gmail → HubSpot Sync")
    print(f"  Gmail query : {query}")
    print(f"  Poll interval: {poll_interval}s" if not args.once else "  Mode: single run")

    gmail_service = build_gmail_service(creds_file, token_file)
    hs_client = HubSpotClient(hs_key)
    db = init_db()

    cycle = 0
    while True:
        cycle += 1
        print(f"\n--- Cycle {cycle} ---")
        try:
            n = run_once(gmail_service, hs_client, db, query)
            print(f"  Done. {n} new contact(s) synced this cycle.")
        except Exception as exc:
            print(f"  Cycle error: {exc}")

        if args.once:
            break

        print(f"  Sleeping {poll_interval}s …")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
