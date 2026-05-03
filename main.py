#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Avvia il loop di monitoraggio continuo della casella Gmail e sincronizza
automaticamente ogni nuovo mittente come contatto in HubSpot.

Uso:
    python main.py

Variabili d'ambiente richieste (file .env):
    HUBSPOT_API_KEY          Token Private App di HubSpot
    GMAIL_CREDENTIALS_FILE   Percorso di credentials.json (default: credentials.json)
    GMAIL_TOKEN_FILE         Percorso per il token OAuth salvato (default: token.json)
    POLL_INTERVAL_SECONDS    Secondi tra un controllo e l'altro (default: 60)
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("sync.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from sync_engine import SyncEngine, SyncResult

HUBSPOT_API_KEY = os.getenv("HUBSPOT_API_KEY", "")
GMAIL_CREDENTIALS_FILE = os.getenv("GMAIL_CREDENTIALS_FILE", "credentials.json")
GMAIL_TOKEN_FILE = os.getenv("GMAIL_TOKEN_FILE", "token.json")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))


# ------------------------------------------------------------------
# Output helpers
# ------------------------------------------------------------------

_COL = {"Creato": "\033[92m", "Aggiornato": "\033[94m", "Errore": "\033[91m"}
_RESET = "\033[0m"


def _colored(status: str) -> str:
    return f"{_COL.get(status, '')}{status}{_RESET}"


def print_report(results: list[SyncResult]) -> None:
    actionable = [r for r in results if r.status != "Ignorato"]
    ignored = sum(1 for r in results if r.status == "Ignorato")

    if not results:
        logger.info("Nessuna nuova email trovata.")
        return

    if not actionable:
        logger.info("Processate %d email — nessun contatto da sincronizzare (%d ignorate).", len(results), ignored)
        return

    separator = "─" * 72
    print(f"\n{separator}")
    print(f"  {'STATO':<12} {'EMAIL CONTATTO':<38} {'ID HUBSPOT'}")
    print(separator)
    for r in actionable:
        status_col = _colored(f"{r.status:<12}")
        email_col = f"{r.email:<38}"
        id_col = r.contact_id or "—"
        print(f"  {status_col} {email_col} {id_col}")
        if r.reason:
            print(f"  {'':12}   ↳ {r.reason}")
    print(separator)
    total = len(actionable)
    created = sum(1 for r in actionable if r.status == "Creato")
    updated = sum(1 for r in actionable if r.status == "Aggiornato")
    errors = sum(1 for r in actionable if r.status == "Errore")
    print(
        f"  Totale: {total}  |  "
        f"Creati: {created}  |  "
        f"Aggiornati: {updated}  |  "
        f"Errori: {errors}  |  "
        f"Ignorate: {ignored}"
    )
    print(f"{separator}\n")


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------

def main() -> None:
    if not HUBSPOT_API_KEY:
        logger.error(
            "HUBSPOT_API_KEY non configurata.\n"
            "Crea un file .env con HUBSPOT_API_KEY=<token> e riavvia."
        )
        sys.exit(1)

    logger.info("Autenticazione Gmail in corso...")
    try:
        gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE).authenticate()
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    hubspot = HubSpotClient(HUBSPOT_API_KEY)
    engine = SyncEngine(gmail, hubspot)

    logger.info(
        "Monitoraggio Gmail avviato — controllo ogni %ds. Premi Ctrl+C per fermare.",
        POLL_INTERVAL,
    )

    while True:
        try:
            logger.info("Controllo nuove email in arrivo...")
            results = engine.run_once()
            print_report(results)

        except KeyboardInterrupt:
            logger.info("Monitoraggio interrotto dall'utente.")
            break

        except Exception as exc:
            logger.exception("Errore imprevisto durante la sincronizzazione: %s", exc)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
