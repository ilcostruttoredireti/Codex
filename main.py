#!/usr/bin/env python3
"""Gmail → HubSpot contact sync — monitoring loop.

Usage:
    python main.py

Environment variables (see .env.example):
    GMAIL_CREDENTIALS_FILE  Path to Google OAuth credentials JSON
    GMAIL_TOKEN_FILE        Path where the OAuth token is stored/cached
    HUBSPOT_ACCESS_TOKEN    HubSpot private app access token
    POLL_INTERVAL           Seconds between Gmail polls (default: 60)
    STATE_FILE              Path to persistent state JSON (default: state.json)
    LOG_LEVEL               Logging level (default: INFO)
"""

import logging
import signal
import sys
import time

import requests
from googleapiclient.errors import HttpError

import config
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state import StateManager
from sync import SyncStatus, process_message

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_running = True


def _handle_signal(signum, _frame):
    global _running
    logger.info(f"Segnale {signum} ricevuto – arresto in corso...")
    _running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
_STATUS_ICON = {
    SyncStatus.CREATED: "✅",
    SyncStatus.UPDATED: "🔄",
    SyncStatus.IGNORED: "⏭️",
}


def _print_result(result) -> None:
    icon = _STATUS_ICON[result.status]
    cid = result.contact_id or "—"
    print(
        f"  {icon}  Stato: {result.status.value:<12} "
        f"Email: {result.email:<40} "
        f"ID HubSpot: {cid}"
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _check_config() -> None:
    if not config.HUBSPOT_ACCESS_TOKEN:
        logger.error("HUBSPOT_ACCESS_TOKEN non impostato. Controlla il file .env")
        sys.exit(1)

    import os
    if not os.path.exists(config.GMAIL_CREDENTIALS_FILE):
        logger.error(
            f"File credenziali Gmail non trovato: {config.GMAIL_CREDENTIALS_FILE}\n"
            "Scarica il file OAuth 2.0 dalla Google Cloud Console e impostalo in .env"
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main() -> None:
    _check_config()

    logger.info("Inizializzazione Gmail client...")
    gmail = GmailClient(config.GMAIL_CREDENTIALS_FILE, config.GMAIL_TOKEN_FILE)

    logger.info("Inizializzazione HubSpot client...")
    hubspot = HubSpotClient(config.HUBSPOT_ACCESS_TOKEN)

    state = StateManager(config.STATE_FILE)

    # On first run, capture baseline historyId without processing old messages
    if state.get_history_id() is None:
        history_id = gmail.get_current_history_id()
        state.set_history_id(history_id)
        state.save()
        logger.info(
            f"Prima esecuzione: baseline historyId={history_id}. "
            "Inizio monitoraggio email future."
        )
    else:
        logger.info(
            f"Ripresa monitoraggio da historyId={state.get_history_id()}"
        )

    logger.info(
        f"Monitoraggio Gmail attivo — polling ogni {config.POLL_INTERVAL}s. "
        "Premi Ctrl+C per fermare."
    )
    print()

    while _running:
        try:
            messages, new_history_id = gmail.get_new_inbox_messages(
                state.get_history_id()
            )

            new_msgs = [m for m in messages if not state.is_processed(m["id"])]

            if new_msgs:
                print(f"[{time.strftime('%H:%M:%S')}] {len(new_msgs)} nuova/e email trovata/e:")
                for msg in new_msgs:
                    result = process_message(msg, gmail, hubspot)
                    _print_result(result)
                    state.mark_processed(msg["id"])
                print()

            state.set_history_id(new_history_id)
            state.save()

        except HttpError as exc:
            if exc.status_code == 410:
                # historyId troppo vecchio: reimposta baseline
                logger.warning(
                    "Gmail historyId scaduto (410). Reset baseline historyId."
                )
                new_id = gmail.get_current_history_id()
                state.set_history_id(new_id)
                state.save()
            else:
                logger.error(f"Errore Gmail API: {exc}")

        except requests.HTTPError as exc:
            logger.error(f"Errore HubSpot API: {exc}")

        except Exception as exc:
            logger.error(f"Errore imprevisto: {exc}", exc_info=True)

        if _running:
            time.sleep(config.POLL_INTERVAL)

    logger.info("Sync fermato. Stato salvato.")


if __name__ == "__main__":
    main()
