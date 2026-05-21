#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora continuamente la casella Gmail e sincronizza i mittenti in HubSpot.

Avvio:
    python main.py

Prima esecuzione: verrà aperto il browser per autorizzare l'accesso Gmail.
"""

import logging
import sys
import time
from datetime import datetime

import config
from gmail_client import get_gmail_service, get_profile_history_id, get_new_messages
from hubspot_client import SyncStatus
from state_manager import load_history_id, save_history_id
from sync import process_senders

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("gmail_hubspot_sync.log"),
    ],
)
logger = logging.getLogger(__name__)

# Colori ANSI per output terminale
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
RESET = "\033[0m"
BOLD = "\033[1m"


def _status_color(status: SyncStatus) -> str:
    if status == SyncStatus.CREATED:
        return GREEN
    if status == SyncStatus.UPDATED:
        return CYAN
    return YELLOW


def print_banner():
    print(f"\n{BOLD}{'─' * 55}")
    print("  Gmail → HubSpot Contact Sync")
    print(f"  Intervallo polling: {config.POLL_INTERVAL_SECONDS}s | Label: {config.GMAIL_LABEL}")
    print(f"{'─' * 55}{RESET}\n")


def run_once(service, history_id: str) -> str:
    """
    Esegue un ciclo di polling e sincronizzazione.
    Restituisce il nuovo history_id.
    """
    senders, new_history_id = get_new_messages(service, history_id)

    if not senders:
        logger.debug("Nessuna nuova email rilevata.")
        return new_history_id

    logger.info("Rilevate %d nuove email da processare.", len(senders))
    report = process_senders(senders)

    # ── Output strutturato ────────────────────────────────────────────────
    print(f"\n{BOLD}[{datetime.now().strftime('%H:%M:%S')}] Processate {report.total} email{RESET}")
    print(f"  Creati: {GREEN}{report.created}{RESET} | "
          f"Aggiornati: {CYAN}{report.updated}{RESET} | "
          f"Ignorati: {YELLOW}{report.ignored}{RESET} | "
          f"Errori: {RED}{report.errors}{RESET}")
    print()

    for result in report.results:
        color = _status_color(result.status)
        contact_id = result.contact_id or "—"
        print(
            f"  {color}[{result.status.value:<10}]{RESET} "
            f"{result.email:<40} "
            f"ID: {contact_id}"
        )
        if result.message and result.message not in ("Nessuna modifica necessaria",):
            print(f"             {result.message}")

    print()
    logger.info(
        "Sync completata — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
        report.created, report.updated, report.ignored, report.errors,
    )

    return new_history_id


def main():
    print_banner()

    # Autenticazione Gmail
    logger.info("Inizializzazione servizio Gmail...")
    try:
        service = get_gmail_service()
        logger.info("Autenticazione Gmail completata.")
    except FileNotFoundError as e:
        logger.error("%s", e)
        sys.exit(1)

    # Verifica token HubSpot
    if not config.HUBSPOT_ACCESS_TOKEN:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN non configurato. "
            "Copia .env.example in .env e inserisci il token."
        )
        sys.exit(1)

    # Carica o inizializza l'history ID
    history_id = load_history_id()
    if history_id:
        logger.info("Riprendendo dal history ID: %s", history_id)
    else:
        history_id = get_profile_history_id(service)
        save_history_id(history_id)
        logger.info("Prima esecuzione. History ID iniziale: %s. "
                    "In attesa di nuove email...", history_id)

    # Loop principale
    logger.info("Monitor avviato. Premi Ctrl+C per fermare.\n")
    try:
        while True:
            try:
                new_history_id = run_once(service, history_id)
                if new_history_id != history_id:
                    history_id = new_history_id
                    save_history_id(history_id)
            except Exception as e:
                logger.error("Errore durante il ciclo di polling: %s", e, exc_info=True)

            time.sleep(config.POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Monitor fermato dall'utente.")
        save_history_id(history_id)
        print(f"\n{YELLOW}Monitor fermato. History ID salvato.{RESET}")


if __name__ == "__main__":
    main()
