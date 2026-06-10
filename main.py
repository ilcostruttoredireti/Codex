"""
Gmail → HubSpot Contact Sync
=============================
Monitors the Gmail inbox and upserts sender contacts into HubSpot CRM.

Usage
-----
  python main.py            # runs continuously (POLL_INTERVAL seconds between cycles)
  python main.py --once     # single cycle then exit

Setup
-----
1. Copy .env.example to .env and fill in your tokens.
2. Download credentials.json from Google Cloud Console (OAuth2 Desktop App).
3. pip install -r requirements.txt
4. python main.py   (a browser window opens for Gmail authorisation on first run)
"""

import argparse
import logging
import os
import time

from dotenv import load_dotenv

load_dotenv()


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _print_cycle_results(results: list) -> None:
    logger = logging.getLogger("main")

    if not results:
        logger.info("Nessuna nuova email da processare.")
        return

    # Per-email output (requested in the spec)
    header = f"{'Stato':<12}  {'Email contatto':<40}  {'ID HubSpot'}"
    logger.info(header)
    logger.info("-" * len(header))
    for r in results:
        logger.info(
            "%-12s  %-40s  %s",
            r.status,
            r.email,
            r.hubspot_id or "N/A",
        )

    created = sum(1 for r in results if r.status == "Creato")
    updated = sum(1 for r in results if r.status == "Aggiornato")
    skipped = sum(1 for r in results if r.status == "Ignorato")
    logger.info(
        "Riepilogo ciclo  Creati: %d  Aggiornati: %d  Ignorati: %d",
        created, updated, skipped,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sincronizza i mittenti Gmail con i contatti HubSpot"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Esegui un solo ciclo e termina",
    )
    args = parser.parse_args()

    _setup_logging(os.getenv("LOG_LEVEL", "INFO"))
    logger = logging.getLogger("main")

    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not hubspot_token:
        raise SystemExit("Errore: HUBSPOT_ACCESS_TOKEN non configurato nel file .env")

    gmail_creds = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    gmail_token = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))
    max_per_cycle = int(os.getenv("MAX_EMAILS_PER_CYCLE", "50"))
    state_file = os.getenv("STATE_FILE", "sync_state.json")
    enable_notes = os.getenv("ENABLE_TIMELINE_NOTES", "true").lower() == "true"

    # Lazy imports so missing dependencies produce a clear error message
    from gmail_client import GmailClient
    from hubspot_client import HubSpotClient
    from state_manager import StateManager
    from sync_engine import SyncEngine

    gmail = GmailClient(credentials_file=gmail_creds, token_file=gmail_token)
    gmail.authenticate()

    hubspot = HubSpotClient(access_token=hubspot_token)
    state = StateManager(state_file=state_file)

    engine = SyncEngine(
        gmail=gmail,
        hubspot=hubspot,
        state=state,
        max_per_cycle=max_per_cycle,
        enable_notes=enable_notes,
    )

    logger.info(
        "Avvio sync Gmail → HubSpot  (intervallo=%ds  note=%s)",
        poll_interval,
        enable_notes,
    )

    while True:
        try:
            results = engine.run_cycle()
            _print_cycle_results(results)
        except KeyboardInterrupt:
            logger.info("Interrotto dall'utente.")
            break
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        if args.once:
            break

        logger.debug("Attendo %ds prima del prossimo ciclo...", poll_interval)
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
