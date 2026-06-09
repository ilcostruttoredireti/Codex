"""Core orchestration: fetch Gmail → extract contact → upsert HubSpot."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List

from .contact_extractor import extract_contact
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str
    message_id: str
    reason: str = ""

    def __str__(self) -> str:
        parts = [
            f"[{self.status.value}]",
            f"email={self.email}",
            f"hs_id={self.contact_id or '-'}",
        ]
        if self.reason:
            parts.append(f"({self.reason})")
        return "  ".join(parts)


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: StateManager,
        batch_size: int = 50,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.state = state
        self.batch_size = batch_size

    def run_once(self) -> List[SyncResult]:
        """
        Fetch new inbox messages since last run, process each sender,
        upsert the contact into HubSpot, and return results.
        """
        messages = self.gmail.list_inbox_messages(
            after_epoch=self.state.last_run_epoch or None,
            max_results=self.batch_size,
        )
        logger.info("Fetched %d messages from Gmail", len(messages))

        results: List[SyncResult] = []

        for msg_stub in messages:
            msg_id = msg_stub["id"]

            if self.state.is_processed(msg_id):
                continue

            result = self._process_message(msg_id)
            results.append(result)
            self.state.mark_processed(msg_id)

        self.state.update_last_run()
        return results

    def _process_message(self, msg_id: str) -> SyncResult:
        try:
            message = self.gmail.get_message_headers(msg_id)
        except Exception as exc:
            logger.warning("Could not fetch message %s: %s", msg_id, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email="",
                contact_id="",
                message_id=msg_id,
                reason=f"gmail fetch error: {exc}",
            )

        from_header = self.gmail.extract_header(message, "From")
        if not from_header:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email="",
                contact_id="",
                message_id=msg_id,
                reason="no From header",
            )

        contact = extract_contact(from_header)
        if contact is None:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=from_header,
                contact_id="",
                message_id=msg_id,
                reason="sender filtered (noreply / invalid)",
            )

        return self._upsert_contact(contact, msg_id)

    def _upsert_contact(self, contact, msg_id: str) -> SyncResult:
        try:
            existing = self.hubspot.find_contact_by_email(contact.email)
        except Exception as exc:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                contact_id="",
                message_id=msg_id,
                reason=f"hubspot search error: {exc}",
            )

        if existing is None:
            try:
                new_id = self.hubspot.create_contact(contact)
                return SyncResult(
                    status=SyncStatus.CREATED,
                    email=contact.email,
                    contact_id=new_id,
                    message_id=msg_id,
                )
            except Exception as exc:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    contact_id="",
                    message_id=msg_id,
                    reason=f"hubspot create error: {exc}",
                )
        else:
            try:
                updated = self.hubspot.update_contact(existing.id, contact, existing)
                status = SyncStatus.UPDATED if updated else SyncStatus.IGNORED
                return SyncResult(
                    status=status,
                    email=contact.email,
                    contact_id=existing.id,
                    message_id=msg_id,
                    reason="" if updated else "nessun campo mancante",
                )
            except Exception as exc:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    contact_id=existing.id,
                    message_id=msg_id,
                    reason=f"hubspot update error: {exc}",
                )
