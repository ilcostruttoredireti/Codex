import logging
import signal
import sys
import time

from . import config
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager
from .sync_engine import SyncEngine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_running = True


def _on_signal(signum, frame):  # noqa: ANN001
    global _running
    logger.info("Segnale ricevuto (%d) – arresto in corso…", signum)
    _running = False


def main() -> None:
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    if not config.HUBSPOT_ACCESS_TOKEN:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN non impostato. "
            "Crea un file .env partendo da .env.example."
        )
        sys.exit(1)

    gmail = GmailClient(config.GMAIL_CREDENTIALS_FILE, config.GMAIL_TOKEN_FILE)
    hubspot = HubSpotClient(config.HUBSPOT_ACCESS_TOKEN)
    state = StateManager(config.STATE_FILE)
    engine = SyncEngine(
        gmail,
        hubspot,
        state,
        max_messages=config.MAX_MESSAGES_PER_POLL,
    )

    logger.info(
        "Gmail → HubSpot sync avviato  (intervallo: %ds)",
        config.POLL_INTERVAL_SECONDS,
    )
    print("-" * 80)

    while _running:
        try:
            results = engine.run_once()
            processed = len(results)
            if processed:
                created = sum(1 for r in results if r.status == "created")
                updated = sum(1 for r in results if r.status == "updated")
                skipped = sum(1 for r in results if r.status == "skipped")
                errors = sum(1 for r in results if r.status == "error")
                logger.info(
                    "Ciclo completato: %d totali  "
                    "[creati=%d  aggiornati=%d  saltati=%d  errori=%d]",
                    processed,
                    created,
                    updated,
                    skipped,
                    errors,
                )
            else:
                logger.debug("Nessun nuovo messaggio in questo ciclo.")
        except Exception:
            logger.exception("Errore nel ciclo di sincronizzazione")

        # Interruptible sleep
        for _ in range(config.POLL_INTERVAL_SECONDS):
            if not _running:
                break
            time.sleep(1)

    logger.info("Sync terminato.")


if __name__ == "__main__":
    main()
