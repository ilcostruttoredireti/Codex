"""
Gmail → HubSpot Contact Sync
-----------------------------
Continuously polls Gmail for new inbound messages and synchronises senders
as contacts in HubSpot (create if new, update missing fields if existing).

Usage:
    python main.py

First run:
    1. Copy .env.example to .env and fill in your credentials.
    2. Download OAuth credentials from Google Cloud Console and save as
       credentials.json (path configurable via GMAIL_CREDENTIALS_FILE).
    3. Run the script — a browser window will open for Gmail authorisation.
       The token is saved to token.json for subsequent runs.
"""

import logging
import sys
import time

import state
import gmail_client
import hubspot_client
import sync_engine
from config import POLL_INTERVAL_SECONDS

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Human-readable status labels for the per-message log lines
_STATUS_LABEL = {
    "created": "CREATO     ",
    "updated": "AGGIORNATO ",
    "skipped": "IGNORATO   ",
    "error":   "ERRORE     ",
}


def main() -> None:
    log.info("=== Gmail → HubSpot Sync avviato ===")

    log.info("Inizializzazione database di stato...")
    state.init_db()

    log.info("Connessione a Gmail (OAuth)...")
    gmail_service = gmail_client.get_gmail_service()

    log.info("Connessione a HubSpot...")
    hs_client = hubspot_client.get_hubspot_client()

    log.info("Ciclo di sync ogni %ds. Ctrl-C per uscire.", POLL_INTERVAL_SECONDS)

    while True:
        try:
            _run_cycle(gmail_service, hs_client)
        except KeyboardInterrupt:
            log.info("Interruzione ricevuta. Uscita.")
            break
        except Exception as exc:
            log.error("Errore imprevisto nel ciclo di sync: %s", exc, exc_info=True)

        log.info("Prossimo controllo tra %ds...", POLL_INTERVAL_SECONDS)
        time.sleep(POLL_INTERVAL_SECONDS)


def _run_cycle(gmail_service, hs_client) -> None:
    log.info("--- Controllo nuove email ---")
    messages = gmail_client.fetch_new_messages(gmail_service)

    if not messages:
        log.info("Nessuna nuova email.")
        return

    log.info("Trovate %d email da processare.", len(messages))

    counts = {"created": 0, "updated": 0, "skipped": 0, "error": 0}

    for msg in messages:
        result = sync_engine.sync_message(hs_client, msg)
        status = result["status"]
        counts[status] = counts.get(status, 0) + 1
        _log_result(result)

    log.info(
        "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d | Errori: %d",
        counts["created"],
        counts["updated"],
        counts["skipped"],
        counts["error"],
    )


def _log_result(result: dict) -> None:
    label = _STATUS_LABEL.get(result["status"], result["status"].upper())
    email = result.get("email") or "N/A"
    contact_id = result.get("contact_id") or "N/A"
    reason = f"  ({result['reason']})" if result.get("reason") else ""
    log.info("  [%s] email=%-35s hubspot_id=%s%s", label, email, contact_id, reason)


if __name__ == "__main__":
    main()
