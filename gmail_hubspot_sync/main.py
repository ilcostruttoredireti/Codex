"""
Orchestratore principale: loop continuo Gmail → HubSpot.

Uso:
    cp ../.env.example ../.env
    # Compilare le variabili in .env
    python main.py

Output per ogni email processata:
    [CREATED|UPDATED|SKIPPED] <email> → HubSpot ID: <id>
"""

import os
import sys
import time
import logging
from dotenv import load_dotenv

# Aggiunge la directory padre al path per trovare .env
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

from gmail_monitor import get_gmail_service, fetch_new_messages
from hubspot_sync import get_hubspot_client, sync_contact

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        log.error("Variabile d'ambiente mancante: %s", var)
        sys.exit(1)
    return val


def process_message(hubspot_client, msg: dict) -> None:
    """Processa un singolo messaggio e stampa il risultato."""
    status, contact_id = sync_contact(
        client=hubspot_client,
        sender_name=msg["sender_name"],
        sender_email=msg["sender_email"],
        domain=msg["domain"],
        subject=msg["subject"],
        add_note=True,
    )

    icon = {"created": "✚", "updated": "↻", "skipped": "–"}.get(status, "?")
    label = status.upper()
    log.info(
        "%s [%s] %s  →  HubSpot ID: %s  |  Oggetto: %s",
        icon, label, msg["sender_email"], contact_id or "n/a", msg["subject"][:60],
    )


def run_sync_loop(
    gmail_service,
    hubspot_client,
    history_id_file: str,
    gmail_label: str,
    poll_interval: int,
) -> None:
    log.info("Monitoraggio Gmail avviato (label=%s, intervallo=%ss)", gmail_label, poll_interval)
    log.info("Premi Ctrl+C per fermare.")

    while True:
        try:
            messages = fetch_new_messages(
                service=gmail_service,
                history_id_file=history_id_file,
                label=gmail_label,
            )

            if messages:
                log.info("Trovate %d nuove email.", len(messages))
                for msg in messages:
                    try:
                        process_message(hubspot_client, msg)
                    except Exception as e:
                        log.warning("Errore processando %s: %s", msg["sender_email"], e)
            else:
                log.debug("Nessuna nuova email.")

        except KeyboardInterrupt:
            log.info("Interruzione manuale. Uscita.")
            break
        except Exception as e:
            log.error("Errore nel ciclo principale: %s", e, exc_info=True)

        time.sleep(poll_interval)


def main() -> None:
    credentials_file = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.getenv("GMAIL_TOKEN_FILE", "token.json")
    history_id_file = os.getenv("LAST_HISTORY_ID_FILE", ".last_history_id")
    gmail_label = os.getenv("GMAIL_LABEL", "INBOX")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    hubspot_token = _require("HUBSPOT_ACCESS_TOKEN")

    log.info("Inizializzazione servizio Gmail…")
    gmail_service = get_gmail_service(credentials_file, token_file)
    log.info("Gmail autenticato.")

    log.info("Inizializzazione client HubSpot…")
    hubspot_client = get_hubspot_client(hubspot_token)
    log.info("HubSpot pronto.")

    run_sync_loop(
        gmail_service=gmail_service,
        hubspot_client=hubspot_client,
        history_id_file=history_id_file,
        gmail_label=gmail_label,
        poll_interval=poll_interval,
    )


if __name__ == "__main__":
    main()
