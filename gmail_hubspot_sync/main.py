import logging
import signal
import sys
import time
from collections import Counter

from .config import load_config
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state import StateManager
from .sync import SyncEngine, SyncStatus


def _setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-7s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


def _print_cycle_summary(results):
    counts = Counter(r.status for r in results)
    actionable = [r for r in results if r.status != SyncStatus.SKIPPED]

    if actionable:
        col_w = (12, 40, 20)
        sep = "─" * (sum(col_w) + 6)
        header = f"{'Stato':<{col_w[0]}}  {'Email':<{col_w[1]}}  {'ID HubSpot'}"
        print(f"\n{sep}")
        print(header)
        print(sep)
        for r in actionable:
            print(
                f"{r.status.value:<{col_w[0]}}  {r.email:<{col_w[1]}}  {r.contact_id or '—'}"
            )
        print(sep)

    parts = []
    for status in (SyncStatus.CREATED, SyncStatus.UPDATED, SyncStatus.SKIPPED, SyncStatus.ERROR):
        if counts[status]:
            parts.append(f"{counts[status]} {status.value.lower()}")
    print("Ciclo: " + " | ".join(parts) if parts else "Ciclo: nessun messaggio")


def main():
    try:
        config = load_config()
    except ValueError as exc:
        print(f"Errore configurazione: {exc}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    # ── Graceful shutdown ─────────────────────────────────────────────────────
    running = True

    def _stop(sig, _frame):
        nonlocal running
        logger.info("Segnale di arresto ricevuto — attendo la fine del ciclo corrente...")
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # ── Inizializzazione client ───────────────────────────────────────────────
    logger.info("═" * 55)
    logger.info("  Gmail → HubSpot Contact Sync")
    logger.info("═" * 55)
    logger.info(f"Polling ogni {config.polling_interval}s  |  Log level: {config.log_level}")

    try:
        gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
        hubspot = HubSpotClient(config.hubspot_access_token)
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Errore inizializzazione client: {exc}")
        sys.exit(1)

    state = StateManager(config.state_file)

    # Recupera l'email dell'account Gmail per escludere email self-sent
    own_email = None
    try:
        profile = gmail.get_profile()
        own_email = profile.get("emailAddress")
        logger.info(f"Account Gmail autenticato: {own_email}")
    except Exception as exc:
        logger.warning(f"Impossibile recuperare profilo Gmail: {exc}")

    engine = SyncEngine(
        gmail=gmail,
        hubspot=hubspot,
        state=state,
        own_email=own_email,
        initial_messages=config.initial_messages,
    )

    # ── Loop principale ───────────────────────────────────────────────────────
    while running:
        try:
            logger.info("▶ Avvio ciclo di sincronizzazione")
            results = engine.run_sync_cycle()
            _print_cycle_summary(results)
        except Exception as exc:
            logger.error(f"Errore nel ciclo di sync: {exc}", exc_info=True)

        if not running:
            break

        logger.info(f"⏳ Prossimo ciclo tra {config.polling_interval}s  (Ctrl+C per uscire)")
        # Sleep interrompibile ogni secondo per rispondere rapidamente a SIGINT
        for _ in range(config.polling_interval):
            if not running:
                break
            time.sleep(1)

    logger.info("Sincronizzazione terminata.")
