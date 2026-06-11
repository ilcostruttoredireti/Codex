#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors the Gmail inbox and syncs each unique sender as a HubSpot contact.

Usage:
    python main.py

Requirements:
    See requirements.txt. Copy .env.example to .env and fill in credentials.
"""

import logging
import sys
import time
from datetime import datetime

from sync.config import (
    GMAIL_CREDENTIALS_FILE,
    HUBSPOT_ACCESS_TOKEN,
    LOG_LEVEL,
    POLL_INTERVAL,
    STATE_FILE,
)
from sync.engine import sync_contact
from sync.gmail_client import (
    authenticate,
    extract_message_info,
    get_message_meta,
    get_new_message_refs,
)
from sync.state_manager import StateManager

# ------------------------------------------------------------------ #
# Logging setup                                                        #
# ------------------------------------------------------------------ #

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("gmail_hubspot_sync")

_STATUS_ICON = {
    "created": "✅ CREATO",
    "updated": "🔄 AGGIORNATO",
    "skipped": "⏭️  IGNORATO",
    "error":   "❌ ERRORE",
}


# ------------------------------------------------------------------ #
# Sync cycle                                                           #
# ------------------------------------------------------------------ #

def run_cycle(gmail_service, state: StateManager) -> None:
    """Fetch new inbox messages and sync each sender to HubSpot."""
    after_unix = state.last_check_unix
    cycle_start = int(time.time())

    logger.info(f"--- Ciclo avviato | Ultima verifica: {datetime.fromtimestamp(after_unix)} ---")

    message_refs = get_new_message_refs(gmail_service, after_unix)
    new_count = 0

    for ref in message_refs:
        message_id: str = ref["id"]

        if state.is_processed(message_id):
            continue

        new_count += 1
        message = get_message_meta(gmail_service, message_id)

        if not message:
            state.mark_processed(message_id)
            continue

        msg_info = extract_message_info(message)

        if not msg_info:
            state.mark_processed(message_id)
            continue

        try:
            result = sync_contact(msg_info)
        except Exception as exc:
            logger.error(f"Errore elaborazione messaggio {message_id}: {exc}", exc_info=True)
            state.mark_processed(message_id)
            continue

        icon = _STATUS_ICON.get(result["status"], "?")
        logger.info(
            f"{icon} | Email: {result['email']} | HubSpot ID: {result['contact_id'] or 'N/A'}"
        )

        state.mark_processed(message_id)
        time.sleep(0.4)  # Respect HubSpot rate limits (250 req/10s)

    state.update_last_check(cycle_start)
    logger.info(f"Ciclo completato: {new_count} email nuove elaborate.")


# ------------------------------------------------------------------ #
# Entry point                                                          #
# ------------------------------------------------------------------ #

def main() -> None:
    print("=" * 60)
    print("  Gmail → HubSpot Contact Sync")
    print("=" * 60)

    if not HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN non configurato. Copia .env.example in .env.")
        sys.exit(1)

    import os
    if not os.path.exists(GMAIL_CREDENTIALS_FILE):
        logger.error(
            f"File credenziali Gmail '{GMAIL_CREDENTIALS_FILE}' non trovato. "
            "Scaricalo da Google Cloud Console."
        )
        sys.exit(1)

    logger.info("Autenticazione Gmail in corso...")
    gmail_service = authenticate()
    logger.info("Gmail autenticato.")

    state = StateManager(STATE_FILE)
    logger.info(f"Stato caricato. Ultima verifica: {datetime.fromtimestamp(state.last_check_unix)}")
    logger.info(f"Intervallo di polling: {POLL_INTERVAL}s")
    logger.info("Avvio monitoraggio continuo... (Ctrl+C per fermare)\n")

    while True:
        try:
            run_cycle(gmail_service, state)
        except KeyboardInterrupt:
            logger.info("Interrotto dall'utente.")
            break
        except Exception as exc:
            logger.error(f"Errore nel ciclo: {exc}", exc_info=True)

        logger.info(f"Prossima verifica tra {POLL_INTERVAL}s...\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
