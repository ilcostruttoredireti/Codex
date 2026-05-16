#!/usr/bin/env python3
"""Gmail → HubSpot contact sync daemon.

Usage:
    python main.py              # continuous polling (default: 60s interval)
    python main.py --once       # single pass then exit
    python main.py --interval 30

Environment variables (see .env.example):
    HUBSPOT_TOKEN, GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE,
    POLL_INTERVAL, GMAIL_LABEL, LOG_LEVEL, STATE_FILE
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("sync.log", encoding="utf-8"),
        ],
    )


def _print_summary(results: list, logger: logging.Logger) -> None:
    from src.sync import Status

    created = [r for r in results if r.status == Status.CREATED]
    updated = [r for r in results if r.status == Status.UPDATED]
    ignored = [r for r in results if r.status == Status.IGNORED]

    separator = "=" * 64
    print(f"\n{separator}")
    print(f"  Sincronizzazione — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Elaborati: {len(results)}  |  "
          f"✅ {len(created)} creati  ♻️ {len(updated)} aggiornati  "
          f"⏭️ {len(ignored)} ignorati")
    print(separator)

    for r in results:
        if r.status != Status.IGNORED or r.email:
            print(f"  {r}")

    print(separator + "\n")
    logger.info(
        f"Ciclo completato: {len(created)} creati, "
        f"{len(updated)} aggiornati, {len(ignored)} ignorati"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync Gmail senders to HubSpot contacts"
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single pass then exit"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("POLL_INTERVAL", "60")),
        help="Polling interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--label",
        default=os.getenv("GMAIL_LABEL", "INBOX"),
        help="Gmail label to monitor (default: INBOX)",
    )
    args = parser.parse_args()

    _setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    logger = logging.getLogger("main")

    # --- Validate config -----------------------------------------------
    hubspot_token = os.getenv("HUBSPOT_TOKEN", "").strip()
    if not hubspot_token:
        logger.error("HUBSPOT_TOKEN is not set. Add it to .env or export it.")
        sys.exit(1)

    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    if not Path(credentials_file).exists():
        logger.error(
            f"Gmail credentials file not found: {credentials_file}\n"
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )
        sys.exit(1)

    # --- Bootstrap clients ---------------------------------------------
    from src.gmail_client import GmailClient
    from src.hubspot_client import HubSpotClient
    from src.state import SyncState
    from src.sync import GmailHubSpotSync

    logger.info("Autenticazione Gmail...")
    gmail = GmailClient(
        credentials_file=credentials_file,
        token_file=os.getenv("GMAIL_TOKEN_FILE", "token.json"),
    )

    logger.info("Connessione HubSpot...")
    hubspot = HubSpotClient(token=hubspot_token)

    state = SyncState(state_file=Path(os.getenv("STATE_FILE", "sync_state.json")))
    syncer = GmailHubSpotSync(gmail=gmail, hubspot=hubspot, state=state)

    # --- Run -----------------------------------------------------------
    mode = "singolo" if args.once else f"continuo (ogni {args.interval}s)"
    logger.info(f"Avvio sync Gmail → HubSpot | modalità: {mode} | label: {args.label}")

    if args.once:
        results = syncer.run_once(label=args.label)
        _print_summary(results, logger)
        return

    while True:
        try:
            logger.debug("--- Polling ---")
            results = syncer.run_once(label=args.label)
            if results:
                _print_summary(results, logger)
            time.sleep(args.interval)
        except KeyboardInterrupt:
            logger.info("Sync interrotto dall'utente")
            break
        except Exception as e:
            logger.error(f"Errore imprevisto: {e}", exc_info=True)
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
