import logging
import os
import time

from dotenv import load_dotenv

from .contact_parser import ContactInfo, parse_sender
from .gmail_client import (
    build_service,
    fetch_initial_messages,
    fetch_messages_since,
    get_message_headers,
)
from .hubspot_client import HubSpotClient
from . import state_manager as sm

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def _sync_contact(hs: HubSpotClient, contact: ContactInfo, message_id: str) -> tuple[str, str]:
    existing = hs.find_contact_by_email(contact.email)

    if existing:
        contact_id = existing["id"]
        p = existing.get("properties", {})
        updates = {}
        if contact.first_name and not p.get("firstname"):
            updates["firstname"] = contact.first_name
        if contact.last_name and not p.get("lastname"):
            updates["lastname"] = contact.last_name
        if contact.company and not p.get("company"):
            updates["company"] = contact.company
        if updates:
            hs.update_contact(contact_id, updates)
        hs.create_note(
            contact_id,
            f"Email inbound ricevuta via Gmail.\nTag: Inbound Gmail\nMessage-ID: {message_id}",
        )
        return "AGGIORNATO", contact_id

    props: dict = {"email": contact.email, "hs_lead_source": "OTHER"}
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company

    contact_id = hs.create_contact(props)
    hs.create_note(
        contact_id,
        f"Contatto creato da email inbound Gmail.\nFonte: Gmail\nTag: Inbound Gmail\nMessage-ID: {message_id}",
    )
    return "CREATO", contact_id


def run() -> None:
    hub_token = os.environ.get("HUBSPOT_ACCESS_TOKEN")
    if not hub_token:
        raise SystemExit("ERRORE: HUBSPOT_ACCESS_TOKEN non impostato nel file .env")

    credentials_file = os.environ.get("GMAIL_CREDENTIALS_FILE", "credentials.json")
    token_file = os.environ.get("GMAIL_TOKEN_FILE", "token.json")
    poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
    lookback_days = int(os.environ.get("INITIAL_LOOKBACK_DAYS", "7"))

    gmail = build_service(credentials_file, token_file)
    hs = HubSpotClient(hub_token)

    logger.info("Avvio monitoraggio Gmail → HubSpot (polling ogni %ds)", poll_interval)

    while True:
        state = sm.load()

        try:
            if state.get("history_id"):
                messages, new_history_id = fetch_messages_since(gmail, state["history_id"])
            else:
                messages, new_history_id = fetch_initial_messages(gmail, lookback_days)
                logger.info("Prima esecuzione: scansione ultimi %d giorni", lookback_days)

            state["history_id"] = new_history_id
            processed_count = 0

            for msg in messages:
                msg_id = msg["id"]
                if sm.is_processed(state, msg_id):
                    continue

                try:
                    headers = get_message_headers(gmail, msg_id)
                    from_header = headers.get("From", "")
                    subject = headers.get("Subject", "(no subject)")

                    contact = parse_sender(from_header)
                    if not contact:
                        sm.mark_processed(state, msg_id)
                        continue

                    status, hs_id = _sync_contact(hs, contact, msg_id)
                    logger.info(
                        "%-10s | %-40s | ID HubSpot: %-12s | %s",
                        status,
                        contact.email,
                        hs_id,
                        subject[:60],
                    )
                except Exception as exc:
                    logger.warning("Errore messaggio %s: %s", msg_id, exc)

                sm.mark_processed(state, msg_id)
                processed_count += 1

            sm.save(state)

            if processed_count:
                logger.info("Processati %d nuovi messaggi.", processed_count)

        except Exception as exc:
            logger.error("Errore ciclo di polling: %s", exc)

        time.sleep(poll_interval)


if __name__ == "__main__":
    run()
