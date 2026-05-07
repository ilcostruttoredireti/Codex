#!/usr/bin/env python3
"""
Gmail → HubSpot Contact Sync
Monitors Gmail inbox and upserts senders into HubSpot CRM.
"""

import os
import re
import sys
import time
import logging
import sqlite3
import argparse
from datetime import datetime
from email.utils import parseaddr

from dotenv import load_dotenv

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
log = logging.getLogger(__name__)

DB_PATH = os.getenv('DB_PATH', 'state.db')
POLL_INTERVAL = int(os.getenv('POLL_INTERVAL_SECONDS', '60'))
GMAIL_QUERY = os.getenv('GMAIL_QUERY', 'in:inbox')
MAX_MESSAGES = int(os.getenv('MAX_MESSAGES_PER_POLL', '100'))

# Patterns for automated/system senders to skip
_SKIP_PATTERNS = re.compile(
    r'^(no[-.]?reply|noreply|mailer[-.]?daemon|postmaster|notifications?'
    r'|donotreply|auto[-.]?reply|bounce|support|info|hello|admin|help'
    r'|newsletter|unsubscribe|alerts?)@',
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
#  State (SQLite)                                                              #
# --------------------------------------------------------------------------- #

def init_db(conn: sqlite3.Connection) -> None:
    conn.execute('''
        CREATE TABLE IF NOT EXISTS processed_messages (
            message_id      TEXT PRIMARY KEY,
            processed_at    TEXT NOT NULL,
            sender_email    TEXT,
            status          TEXT,
            hubspot_id      TEXT
        )
    ''')
    conn.commit()


def is_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute(
        'SELECT 1 FROM processed_messages WHERE message_id = ?', (message_id,)
    ).fetchone() is not None


def mark_processed(
    conn: sqlite3.Connection,
    message_id: str,
    sender_email: str | None,
    status: str,
    hubspot_id: str | None,
) -> None:
    conn.execute(
        '''INSERT OR REPLACE INTO processed_messages
           (message_id, processed_at, sender_email, status, hubspot_id)
           VALUES (?, ?, ?, ?, ?)''',
        (message_id, datetime.utcnow().isoformat(), sender_email, status, hubspot_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
#  Parsing helpers                                                             #
# --------------------------------------------------------------------------- #

def extract_sender(from_header: str) -> tuple[str | None, str | None]:
    """Return (display_name, email) from a raw From header."""
    name, email = parseaddr(from_header)
    if not email or '@' not in email:
        return None, None
    return name.strip() or None, email.lower().strip()


def split_name(display_name: str | None) -> tuple[str | None, str | None]:
    if not display_name:
        return None, None
    parts = display_name.strip().split(None, 1)
    return parts[0], parts[1] if len(parts) > 1 else None


def company_from_domain(domain: str) -> str:
    # Strip common TLDs, capitalise first segment
    return domain.split('.')[0].capitalize()


def should_skip(email: str) -> bool:
    return bool(_SKIP_PATTERNS.match(email))


# --------------------------------------------------------------------------- #
#  Core processing                                                             #
# --------------------------------------------------------------------------- #

def _print_result(status: str, email: str, hubspot_id: str | None) -> None:
    print(
        f"  Stato: {status:<12} | Email: {email:<35} | ID HubSpot: {hubspot_id or '-'}",
        flush=True,
    )


def process_message(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    conn: sqlite3.Connection,
    message_id: str,
    create_notes: bool = True,
) -> None:
    msg = gmail.get_message_headers(message_id)
    if not msg:
        mark_processed(conn, message_id, None, 'ERRORE', None)
        return

    display_name, email = extract_sender(msg['from'])
    if not email:
        log.debug(f"[{message_id}] From header non parsabile: {msg['from']!r}")
        mark_processed(conn, message_id, None, 'IGNORATO', None)
        return

    if should_skip(email):
        log.debug(f"[{message_id}] Mittente automatico ignorato: {email}")
        mark_processed(conn, message_id, email, 'IGNORATO', None)
        _print_result('IGNORATO', email, None)
        return

    domain = email.split('@')[1]
    first_name, last_name = split_name(display_name)
    company = company_from_domain(domain)

    existing = hubspot.find_contact_by_email(email)

    if existing:
        contact_id: str = existing['id']
        props = existing.get('properties', {})
        updates: dict[str, str] = {}

        if first_name and not props.get('firstname'):
            updates['firstname'] = first_name
        if last_name and not props.get('lastname'):
            updates['lastname'] = last_name
        if not props.get('company'):
            updates['company'] = company
        if not props.get('lead_source'):
            updates['lead_source'] = 'Gmail'

        if updates:
            hubspot.update_contact(contact_id, updates)

        if create_notes:
            note_body = (
                f"📧 Email in arrivo ricevuta da {email}\n"
                f"Oggetto: {msg.get('subject', '(nessun oggetto)')}\n"
                f"Data: {msg.get('date', '')}\n"
                f"Tag: Inbound Gmail"
            )
            hubspot.create_note(contact_id, note_body)

        status = 'AGGIORNATO' if updates else 'GIA_PRESENTE'
        mark_processed(conn, message_id, email, status, contact_id)
        _print_result(status, email, contact_id)

    else:
        new_props: dict[str, str] = {'email': email, 'lead_source': 'Gmail'}
        if first_name:
            new_props['firstname'] = first_name
        if last_name:
            new_props['lastname'] = last_name
        if company:
            new_props['company'] = company

        contact_id = hubspot.create_contact(new_props)
        if contact_id:
            if create_notes:
                note_body = (
                    f"📧 Nuovo contatto creato da email in arrivo\n"
                    f"Mittente: {display_name or email} <{email}>\n"
                    f"Oggetto: {msg.get('subject', '(nessun oggetto)')}\n"
                    f"Data: {msg.get('date', '')}\n"
                    f"Tag: Inbound Gmail"
                )
                hubspot.create_note(contact_id, note_body)

            mark_processed(conn, message_id, email, 'CREATO', contact_id)
            _print_result('CREATO', email, contact_id)
        else:
            log.error(f"Impossibile creare contatto per {email}")
            mark_processed(conn, message_id, email, 'ERRORE', None)
            _print_result('ERRORE', email, None)


# --------------------------------------------------------------------------- #
#  Main loop                                                                   #
# --------------------------------------------------------------------------- #

def run(create_notes: bool = True, one_shot: bool = False) -> None:
    hubspot_key = os.environ.get('HUBSPOT_API_KEY')
    if not hubspot_key:
        log.error("HUBSPOT_API_KEY non impostata. Controlla il file .env")
        sys.exit(1)

    gmail = GmailClient()
    hubspot = HubSpotClient(api_key=hubspot_key)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    log.info(f"Gmail → HubSpot Sync avviato (query: '{GMAIL_QUERY}', polling: {POLL_INTERVAL}s)")

    while True:
        try:
            message_ids = gmail.list_inbox_messages(
                max_results=MAX_MESSAGES, query=GMAIL_QUERY
            )
            new_ids = [mid for mid in message_ids if not is_processed(conn, mid)]

            if new_ids:
                print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] Trovati {len(new_ids)} nuovi messaggi da processare")
                for mid in new_ids:
                    process_message(gmail, hubspot, conn, mid, create_notes=create_notes)
            else:
                log.debug("Nessun nuovo messaggio.")

        except KeyboardInterrupt:
            log.info("Interruzione da tastiera. Uscita.")
            break
        except Exception as e:
            log.error(f"Errore nel ciclo di polling: {e}", exc_info=True)

        if one_shot:
            break

        time.sleep(POLL_INTERVAL)

    conn.close()


# --------------------------------------------------------------------------- #
#  CLI                                                                         #
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Sincronizza mittenti Gmail con HubSpot CRM'
    )
    parser.add_argument(
        '--no-notes',
        action='store_true',
        help='Non creare note timeline in HubSpot',
    )
    parser.add_argument(
        '--once',
        action='store_true',
        help='Esegui un singolo ciclo e poi esci (utile per test)',
    )
    parser.add_argument(
        '--reset-db',
        action='store_true',
        help='Azzera il database locale dei messaggi processati',
    )
    args = parser.parse_args()

    if args.reset_db:
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
            print(f"Database {DB_PATH} rimosso.")
        else:
            print("Nessun database da rimuovere.")
        if not args.once:
            return

    run(create_notes=not args.no_notes, one_shot=args.once)


if __name__ == '__main__':
    main()
