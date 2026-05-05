import logging
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Literal, Optional

from contact_parser import parse_sender, is_automated
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

SyncStatus = Literal['created', 'updated', 'ignored', 'error']


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str
    detail: str = ''


class SyncEngine:
    def __init__(self, hubspot: HubSpotClient):
        self._hs = hubspot

    def process_email(self, message: dict) -> SyncResult:
        """Process a single Gmail message (full metadata format) and sync to HubSpot."""
        headers = {
            h['name']: h['value']
            for h in message.get('payload', {}).get('headers', [])
        }
        from_header = headers.get('From', '')
        subject = headers.get('Subject', '(no subject)')
        date_header = headers.get('Date', '')

        if not from_header:
            return SyncResult('ignored', '', '', 'Missing From header')

        sender = parse_sender(from_header)
        email = sender.get('email', '')

        if not email:
            return SyncResult('ignored', '', '', f'Could not parse address from: {from_header!r}')

        if is_automated(email):
            return SyncResult('ignored', email, '', 'Automated/no-reply sender skipped')

        try:
            existing = self._hs.find_contact_by_email(email)

            if existing:
                contact_id = existing['id']
                self._fill_missing_fields(contact_id, existing.get('properties', {}), sender)
                self._add_note(contact_id, email, subject, date_header)
                return SyncResult('updated', email, contact_id)
            else:
                props = self._build_properties(sender)
                new_contact = self._hs.create_contact(props)
                contact_id = new_contact['id']
                self._add_note(contact_id, email, subject, date_header)
                return SyncResult('created', email, contact_id)

        except Exception as exc:
            logger.error("Error processing <%s>: %s", email, exc, exc_info=True)
            return SyncResult('error', email, '', str(exc))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_properties(self, sender: dict) -> dict:
        props: dict = {
            'email': sender['email'],
            'hs_lead_status': 'NEW',
        }
        if sender.get('first_name'):
            props['firstname'] = sender['first_name']
        if sender.get('last_name'):
            props['lastname'] = sender['last_name']
        if sender.get('company'):
            props['company'] = sender['company']
        return props

    def _fill_missing_fields(self, contact_id: str, existing: dict, sender: dict):
        """Patch only fields that are currently blank on the existing contact."""
        updates: dict = {}
        if not existing.get('firstname') and sender.get('first_name'):
            updates['firstname'] = sender['first_name']
        if not existing.get('lastname') and sender.get('last_name'):
            updates['lastname'] = sender['last_name']
        if not existing.get('company') and sender.get('company'):
            updates['company'] = sender['company']
        if updates:
            self._hs.update_contact(contact_id, updates)

    def _add_note(self, contact_id: str, email: str, subject: str, date: str):
        ts_ms: Optional[int] = None
        if date:
            try:
                ts_ms = int(parsedate_to_datetime(date).timestamp() * 1000)
            except Exception:
                pass

        body = (
            f"Email ricevuta via Gmail\n\n"
            f"Da: {email}\n"
            f"Oggetto: {subject}\n"
            f"Tag: Inbound Gmail\n"
            f"Fonte: Gmail\n"
            f"Data: {date or 'n/d'}"
        )
        self._hs.add_note(contact_id, body, ts_ms)
