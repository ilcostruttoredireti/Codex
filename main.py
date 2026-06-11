#!/usr/bin/env python3
"""Gmail → HubSpot Contact Sync — entry point."""

import argparse
import os
import sys
from pathlib import Path


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        print(f"Error: environment variable {name!r} is not set.", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    _load_env()

    parser = argparse.ArgumentParser(
        description="Sincronizza contatti Gmail → HubSpot",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--mode",
        choices=["once", "continuous"],
        default="once",
        help="'once' processa la inbox una volta sola; 'continuous' esegue in loop",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("POLL_INTERVAL_SECONDS", "60")),
        help="Intervallo di polling in secondi (solo modalità continuous)",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=100,
        help="Numero massimo di messaggi da recuperare per ciclo",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula senza scrivere su HubSpot",
    )
    args = parser.parse_args()

    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")
    credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    state_file = os.environ.get("STATE_FILE", "sync_state.json")

    if not Path(credentials_file).exists():
        print(
            f"Error: Gmail credentials file '{credentials_file}' not found.\n"
            "Download it from Google Cloud Console → APIs & Credentials → OAuth 2.0.",
            file=sys.stderr,
        )
        sys.exit(1)

    from gmail_hubspot_sync.gmail_client import GmailClient
    from gmail_hubspot_sync.hubspot_client import HubSpotClient
    from gmail_hubspot_sync.sync import GmailHubSpotSync

    gmail = GmailClient(credentials_file, token_file)
    hubspot = HubSpotClient(hubspot_token)
    sync = GmailHubSpotSync(gmail, hubspot, state_file=state_file, dry_run=args.dry_run)

    if args.mode == "continuous":
        sync.run_continuous(interval=args.interval, batch_size=args.batch)
    else:
        results = sync.run_once(batch_size=args.batch)
        from gmail_hubspot_sync.sync import SyncStatus
        created = sum(1 for r in results if r.status == SyncStatus.CREATED)
        updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
        if not created and not updated:
            print("Nessuna nuova email da processare.")
        else:
            print(f"\nCompletato → Creati: {created} | Aggiornati: {updated}")


if __name__ == "__main__":
    main()
