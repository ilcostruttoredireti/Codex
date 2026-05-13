#!/usr/bin/env python3
"""Entry point: Gmail → HubSpot contact sync."""

import logging
import sys

from gmail_hubspot_sync.config import Config
from gmail_hubspot_sync.gmail_client import GmailClient
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main() -> None:
    try:
        cfg = Config.from_env()
    except EnvironmentError as exc:
        print(f"Errore configurazione:\n{exc}", file=sys.stderr)
        sys.exit(1)

    gmail = GmailClient(cfg.gmail_credentials_file, cfg.gmail_token_file)
    hubspot = HubSpotClient(cfg.hubspot_api_key)
    engine = SyncEngine(gmail, hubspot, cfg.state_file, cfg.poll_interval)

    print(f"Avvio sync Gmail → HubSpot (polling ogni {cfg.poll_interval}s) …")
    engine.run()


if __name__ == "__main__":
    main()
