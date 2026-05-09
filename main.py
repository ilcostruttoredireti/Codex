#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
=============================
Continuously monitors Gmail INBOX for new emails and synchronises
the senders as contacts in HubSpot CRM.

Usage
-----
    python main.py

Environment variables (see .env.example)
-----------------------------------------
    HUBSPOT_ACCESS_TOKEN   – required
    GMAIL_CREDENTIALS_FILE – path to Google OAuth client-secret JSON (default: credentials.json)
    GMAIL_TOKEN_FILE       – where the OAuth token is cached (default: token.json)
    POLL_INTERVAL_SECONDS  – polling interval in seconds (default: 60)
    INITIAL_FETCH_LIMIT    – number of messages to process on first run (default: 50)
    PROCESSED_IDS_FILE     – path to the sync-state file (default: .processed_ids.json)
"""

import sys
import time
from datetime import datetime

import config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync import _load_state, _save_state, run_cycle


def _print_result(r: dict) -> None:
    status = r.get("status", "?")
    email = r.get("email", "unknown")
    hs_id = r.get("hubspot_id") or "-"
    reason = f"  ({r['reason']})" if r.get("reason") else ""
    print(f"    [{status:10}] {email:<40} HubSpot ID: {hs_id}{reason}")


def main() -> None:
    if not config.HUBSPOT_ACCESS_TOKEN:
        sys.exit(
            "ERROR: HUBSPOT_ACCESS_TOKEN is not set.\n"
            "Copy .env.example to .env and fill in your credentials."
        )

    gmail = GmailClient()
    hs = HubSpotClient(config.HUBSPOT_ACCESS_TOKEN)
    state = _load_state(config.PROCESSED_IDS_FILE)

    print("=" * 60)
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Polling every {config.POLL_INTERVAL_SECONDS}s")
    print("=" * 60)

    while True:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{ts}] Checking for new emails …")
        try:
            state, results = run_cycle(gmail, hs, state)
            _save_state(config.PROCESSED_IDS_FILE, state)

            if not results:
                print("    No new messages.")
            else:
                print(f"    Processed {len(results)} message(s):")
                for r in results:
                    _print_result(r)

                created = sum(1 for r in results if r["status"] == "Creato")
                updated = sum(1 for r in results if r["status"] == "Aggiornato")
                ignored = sum(1 for r in results if r["status"] == "Ignorato")
                errors = sum(1 for r in results if r["status"] == "Errore")
                print(
                    f"\n    Summary → Creato: {created}  Aggiornato: {updated}"
                    f"  Ignorato: {ignored}  Errore: {errors}"
                )
        except KeyboardInterrupt:
            print("\nSync stopped by user.")
            sys.exit(0)
        except Exception as exc:
            print(f"    ERROR during sync cycle: {exc}")

        try:
            time.sleep(config.POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\nSync stopped by user.")
            sys.exit(0)


if __name__ == "__main__":
    main()
