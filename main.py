#!/usr/bin/env python3
"""Gmail → HubSpot contact sync.

Monitors the Gmail INBOX continuously and upserts sender contacts into HubSpot.
Uses Gmail history API for efficient polling (no full mailbox scan each cycle).

Usage:
    python main.py

Environment variables (see .env.example):
    GMAIL_CREDENTIALS_FILE  Path to Google OAuth credentials JSON
    GMAIL_TOKEN_FILE        Path where the OAuth token is cached
    HUBSPOT_ACCESS_TOKEN    HubSpot Private App token
    POLL_INTERVAL_SECONDS   Seconds between polls (default 60)
    STATE_FILE              Path for persisting history/processed IDs
"""

import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv

from contact_extractor import extract_contact
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import StateManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# ANSI colours for status output
_COLOURS = {
    'CREATO':     '\033[92m',
    'AGGIORNATO': '\033[94m',
    'IGNORATO':   '\033[93m',
    'ERRORE':     '\033[91m',
}
_RESET = '\033[0m'

_NOTE_BODY = 'Fonte contatto: Gmail | Tag: Inbound Gmail'


def _print_result(result: dict):
    status = result.get('status', '?')
    email = result.get('email', 'N/A')
    contact_id = result.get('contact_id', 'N/A')
    extra = f" | Motivo: {result['reason']}" if 'reason' in result else ''
    colour = _COLOURS.get(status, '')
    print(f"{colour}{status:<12}{_RESET}| Email: {email:<40}| ID HubSpot: {contact_id}{extra}")


def _merge_properties(contact, existing_props: dict) -> dict:
    """Return only the contact fields that are absent or blank in HubSpot."""
    updates = {}
    if contact.first_name and not existing_props.get('firstname'):
        updates['firstname'] = contact.first_name
    if contact.last_name and not existing_props.get('lastname'):
        updates['lastname'] = contact.last_name
    if contact.company and not existing_props.get('company'):
        updates['company'] = contact.company
    return updates


def process_message(gmail: GmailClient, hs: HubSpotClient, message_id: str) -> dict:
    from_header, subject = gmail.get_message_from_header(message_id)

    if not from_header:
        return {'status': 'IGNORATO', 'reason': 'header From assente', 'email': 'N/A', 'contact_id': 'N/A'}

    contact = extract_contact(from_header)
    if not contact:
        return {
            'status': 'IGNORATO',
            'reason': 'mittente automatico o non valido',
            'email': from_header[:60],
            'contact_id': 'N/A',
        }

    existing = hs.find_contact_by_email(contact.email)

    if existing:
        contact_id = existing['id']
        existing_props = existing.get('properties', {})
        updates = _merge_properties(contact, existing_props)
        if updates:
            hs.update_contact(contact_id, updates)
        return {'status': 'AGGIORNATO', 'email': contact.email, 'contact_id': contact_id}

    # New contact
    props = {'email': contact.email}
    if contact.first_name:
        props['firstname'] = contact.first_name
    if contact.last_name:
        props['lastname'] = contact.last_name
    if contact.company:
        props['company'] = contact.company

    contact_id = hs.create_contact(props)
    if not contact_id:
        return {'status': 'ERRORE', 'email': contact.email, 'contact_id': 'N/A', 'reason': 'creazione HubSpot fallita'}

    # Attach source note
    hs.add_note_to_contact(contact_id, _NOTE_BODY)

    return {'status': 'CREATO', 'email': contact.email, 'contact_id': contact_id}


def main():
    credentials_file = os.getenv('GMAIL_CREDENTIALS_FILE', 'credentials.json')
    token_file = os.getenv('GMAIL_TOKEN_FILE', 'token.json')
    hubspot_token = os.getenv('HUBSPOT_ACCESS_TOKEN', '')
    poll_interval = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))
    state_file = os.getenv('STATE_FILE', 'state.json')

    if not hubspot_token:
        logger.error("HUBSPOT_ACCESS_TOKEN non impostato. Controlla il file .env.")
        sys.exit(1)

    gmail = GmailClient(credentials_file, token_file)
    hs = HubSpotClient(hubspot_token)
    state = StateManager(state_file)

    logger.info("Autenticazione Gmail in corso...")
    try:
        gmail.authenticate()
    except FileNotFoundError as exc:
        logger.error(str(exc))
        sys.exit(1)

    if not state.history_id:
        history_id = gmail.get_profile_history_id()
        if not history_id:
            logger.error("Impossibile ottenere il history ID Gmail iniziale.")
            sys.exit(1)
        state.history_id = history_id
        logger.info(f"Primo avvio — history ID corrente: {history_id}")
        logger.info("In attesa di nuove email in arrivo...")
    else:
        logger.info(f"Ripresa dal history ID: {state.history_id}")

    print('-' * 72)
    print(f"{'STATO':<12}| {'EMAIL CONTATTO':<40}| ID HUBSPOT")
    print('-' * 72)

    running = True

    def _stop(sig, frame):
        nonlocal running
        print()
        logger.info("Interruzione ricevuta, chiusura...")
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info(f"Monitoraggio avviato — polling ogni {poll_interval}s. Ctrl+C per fermare.")

    while running:
        try:
            message_ids, new_history_id = gmail.poll_inbox_messages(state.history_id)
            state.history_id = new_history_id

            for msg_id in message_ids:
                if state.is_processed(msg_id):
                    continue
                try:
                    result = process_message(gmail, hs, msg_id)
                    _print_result(result)
                except Exception as exc:
                    logger.error(f"Errore processando messaggio {msg_id}: {exc}")
                finally:
                    state.mark_processed(msg_id)

        except Exception as exc:
            logger.error(f"Errore nel loop principale: {exc}")

        if running:
            time.sleep(poll_interval)

    logger.info("Monitoraggio terminato.")


if __name__ == '__main__':
    main()
