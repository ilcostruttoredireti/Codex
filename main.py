#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitora le email in arrivo e sincronizza i mittenti come contatti HubSpot.

Avvio:
    python main.py

Prima esecuzione:
    Un browser si aprirà per autorizzare l'accesso a Gmail (OAuth2).
    Il token verrà salvato in token.json per le sessioni successive.
"""

import json
import logging
import signal
import sys
import time
from pathlib import Path

import config
from contact_sync import ContactSync
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from models import SyncResult

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_running = True


def _handle_signal(sig, frame) -> None:
    global _running
    logger.info("Shutdown richiesto — arresto al prossimo ciclo")
    _running = False


def _load_state() -> dict:
    path = Path(config.STATE_FILE)
    return json.loads(path.read_text()) if path.exists() else {}


def _save_state(state: dict) -> None:
    Path(config.STATE_FILE).write_text(json.dumps(state))


def _print_result(result: SyncResult) -> None:
    hid = result.hubspot_id or "N/A"
    detail = f" ({result.detail})" if result.detail else ""
    print(f"[{result.status.value:<10}] {result.email:<40} | HubSpot ID: {hid}{detail}")


def run_cycle(sync: ContactSync, gmail: GmailClient, state: dict) -> dict:
    history_id = state.get("history_id")

    if not history_id:
        history_id = gmail.get_current_history_id()
        state["history_id"] = history_id
        _save_state(state)
        logger.info(
            "Prima esecuzione — history ID salvato: %s. "
            "Le email future verranno sincronizzate.",
            history_id,
        )
        return state

    new_history_id = gmail.get_current_history_id()
    processed = 0

    for message in gmail.iter_new_messages(history_id):
        result = sync.process_message(message["id"])
        if result:
            _print_result(result)
            processed += 1

    if processed:
        logger.info("Ciclo completato: %d messaggi elaborati", processed)
    else:
        logger.debug("Nessun nuovo messaggio")

    state["history_id"] = new_history_id
    _save_state(state)
    return state


def main() -> None:
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if not config.HUBSPOT_API_KEY:
        logger.error("HUBSPOT_API_KEY non impostata. Controlla il file .env")
        sys.exit(1)

    gmail = GmailClient()
    gmail.authenticate()

    hubspot = HubSpotClient()
    sync = ContactSync(gmail, hubspot)

    state = _load_state()
    logger.info(
        "Sync avviato — polling ogni %ss", config.POLL_INTERVAL_SECONDS
    )
    print("-" * 70)
    print(f"{'Stato':<12} {'Email':<40} {'HubSpot ID'}")
    print("-" * 70)

    while _running:
        try:
            state = run_cycle(sync, gmail, state)
        except Exception:
            logger.exception("Errore nel ciclo di sync")
        if _running:
            time.sleep(config.POLL_INTERVAL_SECONDS)

    logger.info("Sync terminato")


if __name__ == "__main__":
    main()
