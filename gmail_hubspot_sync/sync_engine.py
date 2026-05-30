import logging
from dataclasses import dataclass
from enum import Enum
from typing import List

from .contact_parser import parse_forwarded_senders, parse_sender
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager

logger = logging.getLogger(__name__)

# Subject prefixes that indicate forwarded messages
_FORWARD_PREFIXES = ("fw:", "fwd:", "i:", "fw :", "fwd :")


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: str
    message_id: str


class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient, state: StateManager):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state = state

    def run_once(self, max_messages: int = 50) -> List[SyncResult]:
        results = []
        # Deduplicate senders across messages within a single run
        seen_senders: set[str] = set()

        for message in self._gmail.fetch_inbox_messages(max_results=max_messages):
            msg_id = message["id"]
            if self._state.is_processed(msg_id):
                continue

            raw_from = self._gmail.get_header(message, "From")
            subject = self._gmail.get_header(message, "Subject")

            contacts = self._extract_contacts(message, raw_from, subject)
            if not contacts:
                logger.debug("Skipping message %s — no parseable sender", msg_id)
                self._state.mark_processed(msg_id)
                continue

            for contact in contacts:
                if contact.email in seen_senders:
                    continue
                seen_senders.add(contact.email)
                result = self._sync_contact(contact)
                results.append(result)
                _log_result(result)

            self._state.mark_processed(msg_id)

        return results

    def _extract_contacts(self, message, raw_from, subject):
        """Return contacts to sync: forwarded senders take priority over the From header."""
        subject_lower = subject.lower().strip()
        is_forwarded = any(subject_lower.startswith(p) for p in _FORWARD_PREFIXES)

        if is_forwarded:
            body = self._gmail.get_plain_body(message)
            contacts = parse_forwarded_senders(body, message["id"], subject)
            if contacts:
                return contacts

        # Fall back to the From header
        contact = parse_sender(raw_from, message["id"], subject)
        return [contact] if contact else []

    def _sync_contact(self, contact) -> SyncResult:
        existing = self._hubspot.find_contact_by_email(contact.email)

        if existing is None:
            created = self._hubspot.create_contact(contact)
            contact_id = created["id"]
            self._hubspot.create_engagement_note(contact_id, contact)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=contact.email,
                hubspot_id=contact_id,
                message_id=contact.message_id,
            )

        contact_id = existing["id"]
        updated = self._hubspot.update_contact(contact_id, contact, existing)
        self._hubspot.create_engagement_note(contact_id, contact)

        changed = updated.get("id") != existing.get("id") or updated != existing
        status = SyncStatus.UPDATED if changed else SyncStatus.IGNORED

        return SyncResult(
            status=status,
            email=contact.email,
            hubspot_id=contact_id,
            message_id=contact.message_id,
        )


def _log_result(r: SyncResult) -> None:
    logger.info(
        "[%s] email=%s  hubspot_id=%s",
        r.status.value,
        r.email,
        r.hubspot_id,
    )
