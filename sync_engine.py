import logging
from typing import Literal

from contact_extractor import extract_contact
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

Status = Literal['created', 'updated', 'ignored']


class SyncResult:
    __slots__ = ('status', 'email', 'contact_id', 'reason')

    def __init__(self, status: Status, email: str, contact_id: str = '', reason: str = ''):
        self.status = status
        self.email = email
        self.contact_id = contact_id
        self.reason = reason

    def __str__(self) -> str:
        base = f'[{self.status.upper()}] {self.email or "(unknown)"}'
        if self.contact_id:
            base += f' | HubSpot ID: {self.contact_id}'
        if self.reason:
            base += f' | {self.reason}'
        return base


class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient):
        self.gmail = gmail
        self.hubspot = hubspot

    # ------------------------------------------------------------------

    def _sync_to_hubspot(self, contact: dict, sender_info: dict) -> tuple:
        """Upsert contact in HubSpot. Returns (status, contact_id)."""
        email = contact['email']
        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing['id']
            props = existing.get('properties', {})

            updates = {}
            if not props.get('firstname') and contact.get('first_name'):
                updates['firstname'] = contact['first_name']
            if not props.get('lastname') and contact.get('last_name'):
                updates['lastname'] = contact['last_name']
            if not props.get('company') and contact.get('company'):
                updates['company'] = contact['company']

            if updates:
                self.hubspot.update_contact(contact_id, updates)
                status: Status = 'updated'
            else:
                status = 'ignored'
        else:
            new_props: dict = {
                'email': email,
                'hs_analytics_source': 'OTHER_CAMPAIGNS',
                'hs_analytics_source_data_1': 'Gmail',
            }
            if contact.get('first_name'):
                new_props['firstname'] = contact['first_name']
            if contact.get('last_name'):
                new_props['lastname'] = contact['last_name']
            if contact.get('company'):
                new_props['company'] = contact['company']

            result = self.hubspot.create_contact(new_props)
            contact_id = result['id']
            status = 'created'

        # Attach a timeline note regardless of create/update/ignored
        try:
            subject = sender_info.get('subject') or '(no subject)'
            date = sender_info.get('date', '')
            note = (
                f'Email ricevuta via Gmail\n'
                f'Oggetto: {subject}\n'
                f'Data: {date}\n'
                f'Tag: Inbound Gmail\n'
                f'Fonte contatto: Gmail'
            )
            self.hubspot.add_note_to_contact(contact_id, note)
        except Exception as exc:
            logger.warning("Could not add note for contact %s: %s", contact_id, exc)

        return status, contact_id

    # ------------------------------------------------------------------

    def process_message(self, message_id: str) -> SyncResult:
        sender_info = self.gmail.get_sender_info(message_id)
        raw_email = sender_info.get('email', '')

        contact = extract_contact(sender_info)
        if not contact:
            return SyncResult('ignored', raw_email, reason='system or invalid address')

        status, contact_id = self._sync_to_hubspot(contact, sender_info)
        return SyncResult(status, contact['email'], contact_id=contact_id)
