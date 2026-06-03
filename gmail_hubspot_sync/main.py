import logging
import sys
import time
from datetime import datetime, timezone

from .config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
    LOG_LEVEL,
    POLL_INTERVAL_SECONDS,
    STATE_DB_PATH,
)
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state import StateManager
from .sync import GmailHubSpotSync


def _setup_logging():
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _validate_config():
    errors = []
    if not HUBSPOT_ACCESS_TOKEN:
        errors.append("HUBSPOT_ACCESS_TOKEN non impostato nel file .env")
    if errors:
        print("\n[ERRORE CONFIGURAZIONE]", file=sys.stderr)
        for e in errors:
            print(f"  • {e}", file=sys.stderr)
        print("\nCopia .env.example in .env e compila i valori richiesti.\n", file=sys.stderr)
        sys.exit(1)


def _print_header(interval: int):
    print(f"\n{'═' * 62}")
    print(f"  Gmail → HubSpot Contact Sync")
    print(f"  Avviato: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"  Polling ogni: {interval}s")
    print(f"{'═' * 62}\n")


def _print_results(results: list, cycle: int):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[Ciclo {cycle:>4} | {ts}] {len(results)} email processate")
    for r in results:
        print(f"  {r}")


def main():
    _setup_logging()
    logger = logging.getLogger("gmail_hubspot_sync")
    _validate_config()

    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
    gmail.authenticate()

    hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)
    state = StateManager(STATE_DB_PATH)
    sync = GmailHubSpotSync(gmail, hubspot, state)

    _print_header(POLL_INTERVAL_SECONDS)
    logger.info("Monitoraggio avviato.")

    cycle = 0
    try:
        while True:
            cycle += 1
            try:
                results = sync.run_once()
                _print_results(results, cycle)
            except Exception as e:
                logger.error(f"Errore nel ciclo {cycle}: {e}", exc_info=True)

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\n\nInterrotto dall'utente (Ctrl+C).")
        stats = state.get_stats()
        print(f"\nRiepilogo sessione: {stats}\n")
        sys.exit(0)


if __name__ == "__main__":
    main()
