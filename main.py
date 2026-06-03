#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync

Monitors the Gmail inbox continuously and upserts sender contacts into HubSpot.
Run once:    python main.py
Run looping: python main.py --loop
"""

import argparse
import logging
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_set(key: str) -> set:
    raw = _env(key)
    return {v.strip().lower() for v in raw.split(",") if v.strip()} if raw else set()


def build_syncer():
    from gmail_hubspot_sync import GmailHubSpotSync

    token = _env("HUBSPOT_ACCESS_TOKEN")
    if not token:
        logger.error("HUBSPOT_ACCESS_TOKEN non impostato in .env")
        sys.exit(1)

    return GmailHubSpotSync(
        gmail_credentials_file=_env("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        gmail_token_file=_env("GMAIL_TOKEN_FILE", "token.json"),
        hubspot_access_token=token,
        state_file=_env("STATE_FILE", ".processed_messages.json"),
        initial_lookback_days=int(_env("INITIAL_LOOKBACK_DAYS", "7")),
        skip_emails=_env_set("SKIP_EMAILS"),
        skip_domains=_env_set("SKIP_DOMAINS"),
    )


def run_once(syncer) -> None:
    results = syncer.run_once()
    syncer.print_summary(results)


def run_loop(syncer, interval_seconds: int) -> None:
    logger.info("Avvio modalità loop — intervallo: %d secondi", interval_seconds)
    while True:
        try:
            run_once(syncer)
        except KeyboardInterrupt:
            logger.info("Interruzione manuale.")
            break
        except Exception as exc:
            logger.error("Errore durante la sincronizzazione: %s", exc, exc_info=True)

        logger.info("Prossima esecuzione tra %d secondi…", interval_seconds)
        try:
            time.sleep(interval_seconds)
        except KeyboardInterrupt:
            logger.info("Interruzione manuale.")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Gmail → HubSpot Contact Sync")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Esegui continuamente (polling ogni POLL_INTERVAL_SECONDS secondi)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(_env("POLL_INTERVAL_SECONDS", "300")),
        help="Secondi tra un ciclo e l'altro (default: 300)",
    )
    args = parser.parse_args()

    syncer = build_syncer()

    if args.loop:
        run_loop(syncer, args.interval)
    else:
        run_once(syncer)


if __name__ == "__main__":
    main()
