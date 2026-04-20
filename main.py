"""
Gmail → HubSpot contact sync
────────────────────────────
Polls Gmail for new inbox messages, extracts sender data, and creates or
updates the corresponding contact in HubSpot.

Usage:
    python main.py

First run:
    1. Copy .env.example → .env and fill in credentials.
    2. Place credentials.json (Google OAuth2 desktop app) in this directory.
    3. Run: python main.py
       A browser window will open for Gmail OAuth consent (once only).
"""

import json
import logging
import os
import sys
import time

import config
from contact_sync import process_email_sender
from gmail_client import (
    HistoryExpiredError,
    get_gmail_service,
    get_initial_state,
    get_message_details,
    get_new_messages,
)

# ── logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

# Max processed IDs kept in state (prevents unbounded file growth)
_MAX_PROCESSED = 10_000


# ── state helpers ─────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if os.path.exists(config.STATE_FILE):
        with open(config.STATE_FILE) as fh:
            return json.load(fh)
    return {}


def _save_state(state: dict) -> None:
    with open(config.STATE_FILE, "w") as fh:
        json.dump(state, fh, indent=2)


# ── per-message processing ────────────────────────────────────────────────────

def _process_message(service, msg_id: str, processed: set) -> None:
    if msg_id in processed:
        return

    msg_data = get_message_details(service, msg_id)
    if not msg_data:
        processed.add(msg_id)
        return

    result = process_email_sender(msg_data)
    _log_result(result)
    processed.add(msg_id)


def _log_result(result: dict) -> None:
    status = result["status"]
    email = result["email"]
    hid = result.get("hubspot_id") or "—"

    icons = {"created": "✓ CREATED ", "updated": "~ UPDATED ", "ignored": "- IGNORED ", "error": "✗ ERROR   "}
    tag = icons.get(status, f"? {status.upper()}")
    logger.info("%-12s %-45s HubSpot ID: %s", tag, email, hid)


# ── main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    if not config.HUBSPOT_ACCESS_TOKEN:
        logger.error(
            "HUBSPOT_ACCESS_TOKEN non configurato. "
            "Copia .env.example in .env e inserisci le credenziali."
        )
        sys.exit(1)

    if not os.path.exists(config.GMAIL_CREDENTIALS_FILE):
        logger.error(
            "File credenziali Gmail non trovato: %s\n"
            "Scarica il file OAuth2 desktop da Google Cloud Console.",
            config.GMAIL_CREDENTIALS_FILE,
        )
        sys.exit(1)

    logger.info("═" * 60)
    logger.info("  Gmail → HubSpot Contact Sync avviato")
    logger.info("  Intervallo polling: %ds", config.POLL_INTERVAL_SECONDS)
    logger.info("═" * 60)

    service = get_gmail_service()
    state = _load_state()
    processed: set = set(state.get("processed_ids", []))

    # ── first-run initialisation ──────────────────────────────────────────────
    if "history_id" not in state:
        logger.info("Prima esecuzione: recupero stato Gmail iniziale…")
        history_id, initial_ids = get_initial_state(service)
        state["history_id"] = history_id
        _save_state(state)

        if config.PROCESS_INITIAL_EMAILS and initial_ids:
            logger.info("Elaborazione di %d email esistenti…", len(initial_ids))
            for mid in initial_ids:
                _process_message(service, mid, processed)
            state["processed_ids"] = list(processed)[-_MAX_PROCESSED:]
            _save_state(state)

    logger.info("In ascolto… (history_id: %s)", state["history_id"])

    # ── polling loop ──────────────────────────────────────────────────────────
    while True:
        try:
            time.sleep(config.POLL_INTERVAL_SECONDS)

            new_ids, new_history_id = get_new_messages(service, state["history_id"])

            if new_ids:
                logger.info("Trovate %d nuove email in inbox", len(new_ids))
                for mid in new_ids:
                    _process_message(service, mid, processed)

                state["history_id"] = new_history_id
                state["processed_ids"] = list(processed)[-_MAX_PROCESSED:]
                _save_state(state)

        except HistoryExpiredError:
            logger.warning("History ID scaduto — reset del cursore Gmail…")
            history_id, _ = get_initial_state(service)
            state["history_id"] = history_id
            _save_state(state)

        except KeyboardInterrupt:
            logger.info("Interruzione utente. Salvataggio stato e uscita…")
            state["processed_ids"] = list(processed)[-_MAX_PROCESSED:]
            _save_state(state)
            sys.exit(0)

        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)


if __name__ == "__main__":
    main()
