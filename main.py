#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Polls Gmail inbox continuously and upserts senders into HubSpot CRM.
"""

import logging
import signal
import sys
import time

from dotenv import load_dotenv

from src.config import Config
from src.sync_engine import SyncEngine

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("data/sync.log"),
    ],
)
logger = logging.getLogger(__name__)


def _shutdown(signum, frame):
    logger.info("Segnale di arresto ricevuto. Uscita in corso…")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    config = Config.from_env()
    engine = SyncEngine(config)

    logger.info(
        "Sync avviato — polling ogni %d s. Ctrl+C per fermare.", config.poll_interval
    )

    while True:
        try:
            results = engine.run_cycle()
            if results:
                logger.info("── Ciclo completato: %d email processate ──", len(results))
                for r in results:
                    hs_id = r.hubspot_id or "—"
                    if r.reason:
                        logger.info(
                            "  [%s] %-45s  HubSpot ID: %-12s  (%s)",
                            r.status,
                            r.email,
                            hs_id,
                            r.reason,
                        )
                    else:
                        logger.info(
                            "  [%s] %-45s  HubSpot ID: %s",
                            r.status,
                            r.email,
                            hs_id,
                        )
            else:
                logger.debug("Nessuna nuova email da processare.")

        except KeyboardInterrupt:
            _shutdown(None, None)
        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        time.sleep(config.poll_interval)


if __name__ == "__main__":
    main()
