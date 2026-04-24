"""
Gmail → HubSpot Contact Sync
============================
Monitora la casella Gmail e sincronizza automaticamente i mittenti su HubSpot.

Avvio:
    python main.py

Variabili d'ambiente richieste (.env):
    HUBSPOT_ACCESS_TOKEN
    GMAIL_CREDENTIALS_FILE  (default: credentials.json)
    GMAIL_TOKEN_FILE        (default: token.json)
    POLL_INTERVAL_SECONDS   (default: 60)
    GMAIL_LABEL             (default: INBOX)
    CONTACT_SOURCE          (default: Gmail)
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime

from dotenv import load_dotenv

from contact_parser import parse_sender
from gmail_reader import GmailReader
from hubspot_sync import HubSpotSync, SyncStatus

load_dotenv()

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("gmail_hubspot_sync.log"),
    ],
)
logger = logging.getLogger("main")


# ------------------------------------------------------------------
# Configurazione
# ------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        logger.error("Variabile d'ambiente mancante: %s", name)
        sys.exit(1)
    return value


# ------------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------------

_running = True

def _handle_signal(signum, frame):
    global _running
    logger.info("Segnale %s ricevuto — arresto in corso...", signum)
    _running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ------------------------------------------------------------------
# Loop principale
# ------------------------------------------------------------------

def process_message(message: dict, syncer: HubSpotSync) -> None:
    from gmail_reader import GmailReader
    from_header = GmailReader.extract_header(message, "From")
    subject = GmailReader.extract_header(message, "Subject")

    if not from_header:
        logger.debug("Messaggio senza header From, ignorato.")
        return

    sender = parse_sender(from_header)
    if not sender:
        logger.debug("Impossibile parsare il mittente: %r", from_header)
        return

    result = syncer.sync_contact(sender)

    # Output strutturato per ogni email processata
    status_icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭️ "}.get(
        result.status.value, "  "
    )
    print(
        f"{status_icon} Stato: {result.status.value:<10} | "
        f"Email: {result.email:<40} | "
        f"ID HubSpot: {result.contact_id or 'N/A':<15} | "
        f"Oggetto: {subject[:50]}"
    )
    logger.info("Elaborato: %s", result)


def run() -> None:
    hubspot_token = _require_env("HUBSPOT_ACCESS_TOKEN")
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    gmail_label = os.getenv("GMAIL_LABEL", "INBOX")
    contact_source = os.getenv("CONTACT_SOURCE", "Gmail")

    logger.info("=" * 60)
    logger.info("Gmail → HubSpot Sync avviato")
    logger.info("Label: %s | Intervallo: %ss | Fonte: %s", gmail_label, poll_interval, contact_source)
    logger.info("=" * 60)

    reader = GmailReader(credentials_file, token_file)
    reader.authenticate()

    syncer = HubSpotSync(hubspot_token, contact_source=contact_source)

    cycle = 0
    while _running:
        cycle += 1
        ts = datetime.now().strftime("%H:%M:%S")
        logger.info("[Ciclo %d — %s] Controllo nuove email...", cycle, ts)

        processed = 0
        try:
            for message in reader.fetch_new_messages(label=gmail_label):
                process_message(message, syncer)
                processed += 1
        except Exception as exc:
            logger.error("Errore durante il fetch: %s", exc, exc_info=True)

        if processed == 0:
            logger.info("Nessuna nuova email.")
        else:
            logger.info("%d email processate.", processed)

        # Attesa prima del prossimo ciclo (interrompibile da segnale)
        for _ in range(poll_interval):
            if not _running:
                break
            time.sleep(1)

    logger.info("Sync terminato.")


if __name__ == "__main__":
    run()
