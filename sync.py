from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Dict, List, Optional

from contact_extractor import ContactInfo, extract_contact

if TYPE_CHECKING:
    from gmail_client import GmailClient
    from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncStatus(Enum):
    CREATED = 'Creato'
    UPDATED = 'Aggiornato'
    SKIPPED = 'Ignorato'
    ERROR = 'Errore'


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    reason: Optional[str] = None

    def __str__(self) -> str:
        parts = [f'[{self.status.value}]', self.email, f'ID={self.contact_id}']
        if self.reason:
            parts.append(f'({self.reason})')
        return ' | '.join(parts)


# ------------------------------------------------------------------
# Property builders
# ------------------------------------------------------------------

def _props_for_create(contact: ContactInfo) -> Dict[str, str]:
    props: Dict[str, str] = {'email': contact.email, 'leadsource': 'Gmail'}
    if contact.first_name:
        props['firstname'] = contact.first_name
    if contact.last_name:
        props['lastname'] = contact.last_name
    if contact.company:
        props['company'] = contact.company
    return props


def _props_for_update(contact: ContactInfo, existing_props: Dict) -> Dict[str, str]:
    """Build a patch dict that fills only empty/missing fields."""
    props: Dict[str, str] = {}

    if contact.first_name and not existing_props.get('firstname'):
        props['firstname'] = contact.first_name
    if contact.last_name and not existing_props.get('lastname'):
        props['lastname'] = contact.last_name
    if contact.company and not existing_props.get('company'):
        props['company'] = contact.company
    # Always record the source if not already set
    if not existing_props.get('leadsource'):
        props['leadsource'] = 'Gmail'

    return props


# ------------------------------------------------------------------
# Core sync logic
# ------------------------------------------------------------------

def sync_message(
    message: Dict,
    hubspot: HubSpotClient,
    add_timeline: bool = True,
) -> SyncResult:
    """Process one Gmail message and sync the sender to HubSpot."""
    from_header = message.get('from', '')
    contact = extract_contact(from_header)

    if not contact:
        return SyncResult(SyncStatus.SKIPPED, from_header, None, 'header non parsabile')

    existing = hubspot.find_by_email(contact.email)

    if existing:
        patch = _props_for_update(contact, existing['properties'])
        if patch:
            ok = hubspot.update_contact(existing['id'], patch)
            if not ok:
                return SyncResult(SyncStatus.ERROR, contact.email, existing['id'], 'aggiornamento fallito')

        if add_timeline:
            hubspot.add_email_note(existing['id'], message.get('subject', ''))

        return SyncResult(SyncStatus.UPDATED, contact.email, existing['id'])

    new_id = hubspot.create_contact(_props_for_create(contact))
    if not new_id:
        return SyncResult(SyncStatus.ERROR, contact.email, None, 'creazione fallita')

    if add_timeline:
        hubspot.add_email_note(new_id, message.get('subject', ''))

    return SyncResult(SyncStatus.CREATED, contact.email, new_id)


def run_sync_cycle(
    gmail: GmailClient,
    hubspot: HubSpotClient,
    add_timeline: bool = True,
) -> List[SyncResult]:
    """Fetch new Gmail messages and sync all senders. Returns per-email results."""
    messages = gmail.get_new_messages()
    results: List[SyncResult] = []

    for msg in messages:
        result = sync_message(msg, hubspot, add_timeline)
        logger.info('%s', result)
        results.append(result)

    return results
