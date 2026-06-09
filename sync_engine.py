import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from config import (
    CHECKPOINT_FILE,
    CREATE_TIMELINE_ACTIVITY,
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_ACCESS_TOKEN,
    MESSAGES_PER_RUN,
)
from contact_parser import ContactInfo, parse_sender
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class Status(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    IGNORED = "ignored"
    ERROR = "error"


@dataclass
class SyncResult:
    status: Status
    email: str
    hubspot_id: Optional[str]
    message_id: str
    reason: Optional[str] = None


class GmailHubSpotSync:
    def __init__(self):
        if not HUBSPOT_ACCESS_TOKEN:
            raise ValueError("HUBSPOT_ACCESS_TOKEN is not set")
        self._gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
        self._hubspot = HubSpotClient(HUBSPOT_ACCESS_TOKEN)
        self._seen: set = self._load_checkpoint()

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def run(self) -> List[SyncResult]:
        """Fetch new inbox messages, sync contacts, return results."""
        messages = self._gmail.list_inbox_messages(max_results=MESSAGES_PER_RUN)
        new_messages = [m for m in messages if m['id'] not in self._seen]

        if not new_messages:
            logger.debug("No new messages")
            return []

        logger.info(f"Processing {len(new_messages)} new message(s)")
        results: List[SyncResult] = []

        for msg_ref in new_messages:
            mid = msg_ref['id']
            result = self._process_message(mid)
            if result:
                results.append(result)
            self._seen.add(mid)

        self._save_checkpoint()
        return results

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _process_message(self, message_id: str) -> Optional[SyncResult]:
        message = self._gmail.get_message_headers(message_id)
        if not message:
            return None

        from_header = GmailClient.get_header(message, 'From')
        if not from_header:
            return SyncResult(Status.IGNORED, 'unknown', None, message_id, 'No From header')

        contact = parse_sender(from_header)
        if not contact:
            return SyncResult(Status.IGNORED, from_header, None, message_id, 'Automated/invalid sender')

        try:
            return self._sync_contact(contact, message_id, message)
        except Exception as exc:
            logger.error(f"Error syncing {contact.email}: {exc}", exc_info=True)
            return SyncResult(Status.ERROR, contact.email, None, message_id, str(exc))

    def _sync_contact(
        self, contact: ContactInfo, message_id: str, message: Dict
    ) -> SyncResult:
        existing = self._hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id = existing['id']
            patch = self._missing_fields(contact, existing['properties'])

            if patch:
                self._hubspot.update_contact(contact_id, patch)
                status = Status.UPDATED
                logger.debug(f"Updated {contact.email} ({contact_id}), fields: {list(patch)}")
            else:
                status = Status.IGNORED
                logger.debug(f"No changes for {contact.email} ({contact_id})")
        else:
            props = self._build_new_props(contact)
            created = self._hubspot.create_contact(props)
            contact_id = created['id']
            status = Status.CREATED
            logger.debug(f"Created {contact.email} ({contact_id})")

        if CREATE_TIMELINE_ACTIVITY and status in (Status.CREATED, Status.UPDATED):
            self._attach_note(contact_id, contact, message)

        return SyncResult(status, contact.email, contact_id, message_id)

    def _build_new_props(self, contact: ContactInfo) -> Dict:
        props: Dict = {'email': contact.email, 'leadsource': 'OTHER'}
        if contact.first_name:
            props['firstname'] = contact.first_name
        if contact.last_name:
            props['lastname'] = contact.last_name
        if contact.company:
            props['company'] = contact.company
        return props

    def _missing_fields(self, contact: ContactInfo, existing: Dict) -> Dict:
        """Return only the properties that are absent in the existing record."""
        patch: Dict = {}
        if contact.first_name and not existing.get('firstname'):
            patch['firstname'] = contact.first_name
        if contact.last_name and not existing.get('lastname'):
            patch['lastname'] = contact.last_name
        if contact.company and not existing.get('company'):
            patch['company'] = contact.company
        return patch

    def _attach_note(
        self, contact_id: str, contact: ContactInfo, message: Dict
    ) -> None:
        subject = GmailClient.get_header(message, 'Subject') or '(nessun oggetto)'
        date = GmailClient.get_header(message, 'Date') or ''
        sender = contact.full_name or contact.email
        body = (
            f"Email in entrata ricevuta via Gmail\n"
            f"Da: {sender} <{contact.email}>\n"
            f"Oggetto: {subject}\n"
            f"Data: {date}\n"
            f"Tag: Inbound Gmail"
        )
        self._hubspot.create_note(contact_id, body)

    # ------------------------------------------------------------------ #
    # Checkpoint persistence                                               #
    # ------------------------------------------------------------------ #

    def _load_checkpoint(self) -> set:
        path = Path(CHECKPOINT_FILE)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                return set(data.get('processed_ids', []))
            except Exception:
                return set()
        return set()

    def _save_checkpoint(self) -> None:
        path = Path(CHECKPOINT_FILE)
        # Limit to last 20 000 IDs to prevent unbounded growth
        ids = list(self._seen)[-20_000:]
        path.write_text(json.dumps({'processed_ids': ids}))
