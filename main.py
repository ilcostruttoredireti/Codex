#!/usr/bin/env python3
"""Gmail → HubSpot contact sync.

Run this script to start the continuous sync loop.  On the first run it will
open a browser window for Gmail OAuth authorisation and save a token to disk.
Subsequent runs reuse the saved token automatically.

Required environment variables (see .env.example):
    HUBSPOT_ACCESS_TOKEN   — HubSpot Private App token
    GMAIL_CREDENTIALS_FILE — path to credentials.json from Google Cloud Console
"""

import sys

from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
)
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync_engine import SyncEngine


def main() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        print("ERROR: HUBSPOT_ACCESS_TOKEN is not set. Check your .env file.")
        sys.exit(1)

    if not GMAIL_CREDENTIALS_FILE:
        print("ERROR: GMAIL_CREDENTIALS_FILE is not set. Check your .env file.")
        sys.exit(1)

    print("[Init] Authenticating with Gmail…")
    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)

    print("[Init] Connecting to HubSpot…")
    hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)

    engine = SyncEngine(gmail, hubspot)
    engine.run_forever()


if __name__ == "__main__":
    main()
