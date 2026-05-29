"""
Gmail → HubSpot contact sync daemon.

Usage:
    python -m gmail_hubspot_sync.main          # run once
    python -m gmail_hubspot_sync.main --loop   # poll continuously
"""

import argparse
import logging
import sys
import time

from .config import Config
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager
from .sync_engine import SyncEngine, SyncStatus


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, Config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def build_engine() -> SyncEngine:
    gmail = GmailClient(
        credentials_file=Config.GMAIL_CREDENTIALS_FILE,
        token_file=Config.GMAIL_TOKEN_FILE,
        scopes=Config.GMAIL_SCOPES,
    )
    gmail.authenticate()

    hubspot = HubSpotClient(api_key=Config.HUBSPOT_API_KEY)
    state = StateManager(state_file=Config.STATE_FILE)

    return SyncEngine(gmail=gmail, hubspot=hubspot, state=state)


def run_once(engine: SyncEngine) -> None:
    logger = logging.getLogger(__name__)
    results = engine.run_once()

    if not results:
        logger.info("Nessuna nuova email da processare.")
        return

    counts = {s: 0 for s in SyncStatus}
    for r in results:
        counts[r.status] += 1
        logger.info(str(r))

    logger.info(
        "Riepilogo: %d creati | %d aggiornati | %d ignorati",
        counts[SyncStatus.CREATED],
        counts[SyncStatus.UPDATED],
        counts[SyncStatus.IGNORED],
    )


def main() -> None:
    setup_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="Gmail → HubSpot contact sync")
    parser.add_argument(
        "--loop", action="store_true",
        help=f"Poll continuously every {Config.POLL_INTERVAL_SECONDS}s"
    )
    args = parser.parse_args()

    if not Config.HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY non configurato. Controlla il file .env.")
        sys.exit(1)

    engine = build_engine()

    if args.loop:
        logger.info(
            "Avvio monitoraggio continuo (intervallo: %ds)", Config.POLL_INTERVAL_SECONDS
        )
        while True:
            try:
                run_once(engine)
            except Exception as e:
                logger.error("Errore durante la sincronizzazione: %s", e, exc_info=True)
            time.sleep(Config.POLL_INTERVAL_SECONDS)
    else:
        run_once(engine)


if __name__ == "__main__":
    main()
