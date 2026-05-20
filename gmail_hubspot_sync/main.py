"""
Gmail → HubSpot Contact Sync
=============================
Monitora la casella Gmail in arrivo e sincronizza automaticamente i mittenti
come contatti HubSpot (creando nuovi o aggiornando quelli esistenti).

Avvio:
    python main.py

Setup iniziale: vedi .env.example per le variabili richieste.
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state import SyncState
from filters import should_skip as _should_skip

load_dotenv()

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ── display helpers ───────────────────────────────────────────────────────────

_ICONS = {"CREATO": "✅", "AGGIORNATO": "🔄", "IGNORATO": "⏭️"}
_COL = {"CREATO": "\033[32m", "AGGIORNATO": "\033[33m", "IGNORATO": "\033[90m", "RESET": "\033[0m"}


def _print_result(result: dict) -> None:
    s = result["status"]
    c = _COL.get(s, "")
    r = _COL["RESET"]
    print(f"{_ICONS.get(s, '•')} {c}{s:12}{r} │ {result['email']:42} │ HubSpot ID: {result['hubspot_id'] or '—'}")


# ── core sync cycle ───────────────────────────────────────────────────────────

def sync_cycle(gmail: GmailClient, hubspot: HubSpotClient, state: SyncState) -> int:
    """Fetch new Gmail messages, sync senders to HubSpot. Returns # processed."""
    msg_ids, new_history_id = gmail.get_new_message_ids(state.history_id)

    processed = 0
    for msg_id in msg_ids:
        if state.is_processed(msg_id):
            continue

        try:
            sender = gmail.get_message_sender(msg_id)
            if not sender:
                continue

            email = sender["email"]
            if _should_skip(email):
                logger.debug(f"Skipped (automated): {email}")
                state.mark_processed(msg_id)
                continue

            result = hubspot.sync_sender(
                email=email,
                name=sender["name"],
                subject=sender["subject"],
                date=sender["date"],
            )
            _print_result(result)
            logger.info(f"{result['status']} | {email} | ID={result['hubspot_id']}")
            processed += 1

        except Exception as exc:
            logger.error(f"Errore su messaggio {msg_id}: {exc}", exc_info=True)
        finally:
            state.mark_processed(msg_id)

    if new_history_id:
        state.history_id = new_history_id

    return processed


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        logger.error("HUBSPOT_ACCESS_TOKEN mancante. Copia .env.example in .env e compila i valori.")
        sys.exit(1)

    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

    gmail = GmailClient(
        credentials_file=os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json"),
        token_file=os.getenv("GMAIL_TOKEN_FILE", "token.pickle"),
    )
    hubspot = HubSpotClient(token)
    state = SyncState(os.getenv("STATE_FILE", "sync_state.json"))

    # First run: record the current historyId without replaying old emails.
    if not state.history_id:
        h_id = gmail.get_current_history_id()
        state.history_id = h_id
        logger.info(f"Prima esecuzione — history ID inizializzato a {h_id}.")
        logger.info("Le nuove email arriveranno dal prossimo ciclo di polling.")

    logger.info(f"Gmail → HubSpot sync avviato | polling ogni {poll_interval}s")
    print("\n" + "═" * 72)
    print(f"  {'STATO':12} │ {'EMAIL':42} │ {'HUBSPOT ID'}")
    print("═" * 72)

    while True:
        try:
            n = sync_cycle(gmail, hubspot, state)
            if n:
                logger.info(f"Ciclo completato: {n} email processate.")
        except Exception as exc:
            logger.error(f"Errore nel ciclo di sync: {exc}", exc_info=True)

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
