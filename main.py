#!/usr/bin/env python3
"""
Gmail → HubSpot contact sync daemon.

Usage:
    cp .env.example .env          # fill in your tokens
    pip install -r requirements.txt
    python main.py

On the first run you will be asked to authorise Gmail access in your browser.
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync import SyncResult, process_email

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(path: str, state: dict) -> None:
    Path(path).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run() -> None:
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "").strip()
    if not hubspot_token:
        sys.exit("Errore: HUBSPOT_ACCESS_TOKEN non impostato nel file .env")

    poll_interval = int(os.getenv("POLL_INTERVAL", "60"))
    initial_messages = int(os.getenv("INITIAL_MESSAGES", "10"))
    add_timeline = os.getenv("ADD_TIMELINE_NOTES", "true").lower() == "true"
    state_file = os.getenv("STATE_FILE", "gmail_sync_state.json")

    log.info("Inizializzazione Gmail client…")
    gmail = GmailClient()
    log.info("Inizializzazione HubSpot client…")
    hubspot = HubSpotClient(hubspot_token)

    state = _load_state(state_file)
    processed: set[str] = set(state.get("processed_ids", []))
    history_id: str | None = state.get("history_id")

    # First run: seed state and optionally process recent messages
    if not history_id:
        log.info("Prima esecuzione — recupero storia Gmail…")
        if initial_messages > 0:
            msg_ids = gmail.get_recent_message_ids(max_results=initial_messages)
            log.info("Processo %d email recenti…", len(msg_ids))
            _process_batch(msg_ids, processed, gmail, hubspot, add_timeline)

        history_id = gmail.get_current_history_id()
        _persist(state_file, processed, history_id)
        log.info("Stato iniziale salvato (historyId=%s). Inizio monitoraggio.", history_id)

    log.info(
        "Monitoraggio attivo — polling ogni %ds  (Ctrl+C per uscire)", poll_interval
    )

    while True:
        try:
            new_ids, history_id = gmail.get_new_messages_since(history_id)

            if new_ids:
                log.info("%d nuova/e email trovata/e", len(new_ids))
                _process_batch(new_ids, processed, gmail, hubspot, add_timeline)
                _persist(state_file, processed, history_id)
            else:
                log.debug("Nessuna nuova email.")

        except KeyboardInterrupt:
            log.info("Interruzione manuale. Uscita.")
            break
        except Exception as exc:
            log.error("Errore durante il polling: %s", exc, exc_info=True)

        time.sleep(poll_interval)


def _process_batch(
    message_ids: list[str],
    processed: set[str],
    gmail: GmailClient,
    hubspot: HubSpotClient,
    add_timeline: bool,
) -> None:
    for msg_id in message_ids:
        if msg_id in processed:
            log.debug("Messaggio %s già processato, skip.", msg_id)
            continue

        sender = gmail.get_message_sender(msg_id)
        if sender is None:
            processed.add(msg_id)
            continue

        try:
            result: SyncResult = process_email(sender, hubspot, add_timeline)
        except Exception as exc:
            log.error("Errore sync per %s: %s", sender.get("email"), exc)
            continue

        _print_result(result)
        processed.add(msg_id)


def _print_result(r: SyncResult) -> None:
    symbol = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️ "}.get(r.status, "❓")
    id_part = f"  HubSpot ID: {r.hubspot_id}" if r.hubspot_id else ""
    reason_part = f"  ({r.reason})" if r.reason else ""
    log.info("%s %s  %s%s%s", symbol, r.status, r.email, id_part, reason_part)


def _persist(state_file: str, processed: set[str], history_id: str) -> None:
    # Keep only the last 5 000 IDs to bound file size
    trimmed = list(processed)[-5000:]
    _save_state(state_file, {"history_id": history_id, "processed_ids": trimmed})


if __name__ == "__main__":
    run()
