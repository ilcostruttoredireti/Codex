import logging
import time
import sys

from config import POLL_INTERVAL_SECONDS, HUBSPOT_ACCESS_TOKEN
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync import run_cycle

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _check_config() -> None:
    if not HUBSPOT_ACCESS_TOKEN:
        raise SystemExit(
            "HUBSPOT_ACCESS_TOKEN non configurato. "
            "Imposta la variabile d'ambiente nel file .env."
        )


def main() -> None:
    _check_config()
    logger.info("=== Gmail → HubSpot Contact Sync avviato ===")
    logger.info("Intervallo di polling: %ds", POLL_INTERVAL_SECONDS)

    gmail = GmailClient()
    hubspot = HubSpotClient()

    cycle = 0
    while True:
        cycle += 1
        logger.info("--- Ciclo #%d ---", cycle)
        try:
            results = run_cycle(gmail, hubspot)
            if results:
                print()
                print(f"  {'Stato':<12}  {'Email':<40}  ID HubSpot")
                print("  " + "-" * 70)
                for r in results:
                    print(f"  {r.status:<12}  {r.email:<40}  {r.contact_id or '—'}")
                print()
            else:
                logger.info("Nessuna email nuova in questo ciclo.")
        except KeyboardInterrupt:
            logger.info("Interruzione manuale. Uscita.")
            break
        except Exception as exc:
            logger.exception("Errore imprevisto nel ciclo: %s", exc)

        logger.info("Prossimo controllo tra %ds...", POLL_INTERVAL_SECONDS)
        try:
            time.sleep(POLL_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            logger.info("Interruzione manuale. Uscita.")
            break


if __name__ == "__main__":
    main()
