"""
Gmail → HubSpot contact sync — main entry point.

Usage:
    python sync.py

Environment (see .env.example):
    HUBSPOT_ACCESS_TOKEN   HubSpot private-app token
    GOOGLE_CREDENTIALS_FILE  path to OAuth credentials JSON (default: credentials.json)
    POLL_INTERVAL_SECONDS    seconds between polls (default: 60)
    STATE_FILE               JSON file persisting historyId (default: sync_state.json)
    LOG_LEVEL                logging level (default: INFO)
"""

import json
import logging
import os
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

HUBSPOT_ACCESS_TOKEN: str = os.environ["HUBSPOT_ACCESS_TOKEN"]
CREDENTIALS_FILE: str = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
TOKEN_FILE: str = "token.json"
STATE_FILE: str = os.getenv("STATE_FILE", "sync_state.json")
POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# ---------------------------------------------------------------------------
# State persistence (historyId + processed set)
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    if Path(STATE_FILE).exists():
        with open(STATE_FILE) as fh:
            return json.load(fh)
    return {"history_id": None, "processed": []}


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_running = True


def _handle_signal(sig, _frame):
    global _running
    logger.info("Signal %s received — shutting down after current batch.", sig)
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def _print_result(result: dict) -> None:
    status = result["status"]
    email = result["email"]
    cid = result["contact_id"] or "—"
    tag = {"Creato": "✚", "Aggiornato": "↑", "Ignorato": "·", "Errore": "✖"}.get(status, "?")
    logger.info("%s  %-10s  %-40s  HubSpot ID: %s", tag, status, email, cid)


def run() -> None:
    import gmail_client as gmail
    import hubspot_client as hs

    logger.info("=== Gmail → HubSpot sync avviato ===")
    logger.info("Credenziali Gmail : %s", CREDENTIALS_FILE)
    logger.info("Polling ogni      : %ds", POLL_INTERVAL)

    gmail_svc = gmail.build_service(CREDENTIALS_FILE, TOKEN_FILE)
    hs_client = hs.build_client(HUBSPOT_ACCESS_TOKEN)

    state = _load_state()
    processed: set[str] = set(state.get("processed", []))

    # First run: anchor to current historyId so we only process future mail
    if state["history_id"] is None:
        state["history_id"] = gmail.get_initial_history_id(gmail_svc)
        _save_state(state)
        logger.info("Primo avvio: storico ancorato a historyId=%s", state["history_id"])
        logger.info("In attesa di nuove email…")

    while _running:
        try:
            new_ids, latest_history_id = gmail.fetch_new_message_ids(
                gmail_svc, state["history_id"]
            )

            # Filter already-processed (safety net against duplicates)
            new_ids = [mid for mid in new_ids if mid not in processed]

            if new_ids:
                logger.info("Nuove email da processare: %d", len(new_ids))

            counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0, "Errore": 0}

            for msg_id in new_ids:
                sender = gmail.parse_sender(gmail_svc, msg_id)
                processed.add(msg_id)

                if sender is None:
                    counts["Ignorato"] += 1
                    continue

                result = hs.upsert_contact(hs_client, sender)
                _print_result(result)
                counts[result["status"]] = counts.get(result["status"], 0) + 1

            if new_ids:
                logger.info(
                    "Riepilogo batch — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
                    counts["Creato"], counts["Aggiornato"], counts["Ignorato"], counts["Errore"],
                )

            # Persist updated state (cap processed list at 10 000 to avoid unbounded growth)
            state["history_id"] = latest_history_id
            state["processed"] = list(processed)[-10_000:]
            _save_state(state)

        except Exception as exc:
            logger.error("Errore nel ciclo di polling: %s", exc, exc_info=True)

        if _running:
            time.sleep(POLL_INTERVAL)

    logger.info("Sync terminato.")


if __name__ == "__main__":
    missing = []
    if not os.environ.get("HUBSPOT_ACCESS_TOKEN"):
        missing.append("HUBSPOT_ACCESS_TOKEN")
    if not Path(os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")).exists():
        missing.append(f"File credenziali Gmail '{CREDENTIALS_FILE}' non trovato")
    if missing:
        for m in missing:
            logger.error("Configurazione mancante: %s", m)
        sys.exit(1)

    run()
