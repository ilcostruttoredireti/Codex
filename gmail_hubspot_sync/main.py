"""
Gmail → HubSpot Contact Sync
Monitora l'inbox Gmail e sincronizza i mittenti su HubSpot.

Utilizzo:
    python -m gmail_hubspot_sync.main

Configurazione tramite .env (vedi .env.example nella root).
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .contact_parser import ContactInfo, is_system_sender, parse_sender
from .gmail_client import GmailClient, HistoryExpiredError
from .hubspot_client import HubSpotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Stato persistente
# ------------------------------------------------------------------

_DEFAULT_STATE: dict = {"history_id": None, "processed_ids": []}


def _load_state(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            logger.warning("File di stato corrotto, si riparte da zero.")
    return dict(_DEFAULT_STATE)


def _save_state(path: str, state: dict) -> None:
    # Mantieni al massimo 2000 ID processati per evitare crescita illimitata
    state["processed_ids"] = state.get("processed_ids", [])[-2000:]
    Path(path).write_text(json.dumps(state, indent=2))


# ------------------------------------------------------------------
# Logica di sincronizzazione singola email
# ------------------------------------------------------------------

def _sync_message(
    gmail: GmailClient,
    hs: HubSpotClient,
    message_id: str,
    processed_ids: set[str],
) -> dict:
    """
    Processa un messaggio e sincronizza il mittente su HubSpot.
    Restituisce un dict con stato, email e hubspot_id.
    """
    if message_id in processed_ids:
        return {"status": "Ignorato", "reason": "già processato", "email": None, "hubspot_id": None}

    try:
        msg = gmail.get_message_metadata(message_id)
    except Exception as exc:
        logger.warning("Impossibile recuperare messaggio %s: %s", message_id, exc)
        return {"status": "Ignorato", "reason": "errore Gmail", "email": None, "hubspot_id": None}

    from_header = gmail.header(msg, "From")
    if not from_header:
        return {"status": "Ignorato", "reason": "nessun mittente", "email": None, "hubspot_id": None}

    contact: Optional[ContactInfo] = parse_sender(from_header)
    if not contact:
        return {"status": "Ignorato", "reason": "email non parsabile", "email": None, "hubspot_id": None}

    if is_system_sender(contact.email):
        return {"status": "Ignorato", "reason": "mittente automatico", "email": contact.email, "hubspot_id": None}

    subject = gmail.header(msg, "Subject") or "(nessun oggetto)"
    date_str = gmail.header(msg, "Date") or ""

    note_body = (
        f"📧 Email ricevuta via Gmail\n"
        f"Oggetto: {subject}\n"
        f"Data: {date_str}\n"
        f"Tag: Inbound Gmail"
    )

    existing = hs.find_contact_by_email(contact.email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        updates = HubSpotClient.build_update_properties(contact, existing_props)
        if updates:
            hs.update_contact(contact_id, updates)
        hs.create_note_for_contact(contact_id, note_body)
        return {"status": "Aggiornato", "email": contact.email, "hubspot_id": contact_id}

    # Nuovo contatto
    create_props: dict = {
        "email": contact.email,
        "hs_lead_source": "Gmail",
    }
    if contact.first_name:
        create_props["firstname"] = contact.first_name
    if contact.last_name:
        create_props["lastname"] = contact.last_name
    if contact.company:
        create_props["company"] = contact.company

    new_id = hs.create_contact(create_props)
    if new_id:
        hs.create_note_for_contact(new_id, note_body)
        return {"status": "Creato", "email": contact.email, "hubspot_id": new_id}

    return {"status": "Ignorato", "reason": "errore HubSpot", "email": contact.email, "hubspot_id": None}


# ------------------------------------------------------------------
# Loop principale
# ------------------------------------------------------------------

def run() -> None:
    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not hubspot_token:
        raise RuntimeError("HUBSPOT_ACCESS_TOKEN mancante nel file .env")

    gmail_creds = os.getenv("GMAIL_CREDENTIALS_PATH", "credentials.json")
    gmail_token = os.getenv("GMAIL_TOKEN_PATH", "token.json")
    poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
    state_file = os.getenv("STATE_FILE_PATH", "sync_state.json")
    lookback_days = int(os.getenv("INITIAL_LOOKBACK_DAYS", "7"))

    gmail = GmailClient(gmail_creds, gmail_token)
    hs = HubSpotClient(hubspot_token)

    logger.info("═" * 60)
    logger.info("  Gmail → HubSpot Contact Sync avviato")
    logger.info("  Intervallo polling: %ds", poll_interval)
    logger.info("═" * 60)

    while True:
        state = _load_state(state_file)
        processed_set: set[str] = set(state.get("processed_ids", []))
        history_id: Optional[str] = state.get("history_id")

        try:
            # Primo avvio o historyId scaduto → full scan
            if not history_id:
                logger.info("Primo avvio: scansione inbox ultimi %d giorni…", lookback_days)
                message_ids, new_history_id = gmail.list_inbox_messages_since_days(lookback_days)
            else:
                try:
                    message_ids, new_history_id = gmail.list_new_inbox_messages(history_id)
                except HistoryExpiredError:
                    logger.warning("History ID scaduto. Eseguo full scan di recupero…")
                    message_ids, new_history_id = gmail.list_inbox_messages_since_days(lookback_days)

            if message_ids:
                logger.info("Trovati %d messaggi da elaborare.", len(message_ids))

            counts = {"Creato": 0, "Aggiornato": 0, "Ignorato": 0}

            for msg_id in message_ids:
                result = _sync_message(gmail, hs, msg_id, processed_set)
                status = result["status"]
                counts[status] = counts.get(status, 0) + 1
                processed_set.add(msg_id)

                if status != "Ignorato":
                    logger.info(
                        "[%s] %-40s → HubSpot ID: %s",
                        status,
                        result["email"],
                        result["hubspot_id"],
                    )

            if message_ids:
                logger.info(
                    "Riepilogo ciclo → Creati: %d | Aggiornati: %d | Ignorati: %d",
                    counts["Creato"],
                    counts["Aggiornato"],
                    counts["Ignorato"],
                )

            # Persiste nuovo stato
            state["history_id"] = new_history_id
            state["processed_ids"] = list(processed_set)
            _save_state(state_file, state)

        except Exception as exc:
            logger.error("Errore nel ciclo di sync: %s", exc, exc_info=True)

        logger.debug("Prossima sincronizzazione tra %ds…", poll_interval)
        time.sleep(poll_interval)


if __name__ == "__main__":
    run()
