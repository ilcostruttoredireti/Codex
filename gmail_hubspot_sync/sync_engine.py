import logging
from typing import List

from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .models import ContactInfo, SyncResult, SyncStatus

logger = logging.getLogger(__name__)


class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient, max_per_cycle: int = 50):
        self._gmail = gmail
        self._hubspot = hubspot
        self._max_per_cycle = max_per_cycle

    def run_cycle(self) -> List[SyncResult]:
        """Process one batch of unsynced inbox messages and return results."""
        raw_messages = self._gmail.get_unprocessed_inbox_messages(self._max_per_cycle)
        if not raw_messages:
            logger.debug("No new messages to process")
            return []

        results: List[SyncResult] = []
        for msg_stub in raw_messages:
            msg_id = msg_stub["id"]
            result = self._process_message(msg_id)
            results.append(result)
            # Always mark as processed so we don't retry on transient errors infinitely
            try:
                self._gmail.mark_as_processed(msg_id)
            except Exception as exc:
                logger.warning("Could not label message %s: %s", msg_id, exc)

        return results

    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult:
        sender_data = self._gmail.get_message_sender(message_id)
        if not sender_data:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email="unknown",
                hubspot_id=None,
                message_id=message_id,
                reason="could not fetch message",
            )

        from_header = sender_data["from"]
        contact = GmailClient.parse_from_header(from_header)

        if contact is None:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=from_header,
                hubspot_id=None,
                message_id=message_id,
                reason="sender filtered or unparseable",
            )

        return self._upsert_hubspot_contact(contact, message_id)

    def _upsert_hubspot_contact(self, contact: ContactInfo, message_id: str) -> SyncResult:
        try:
            existing = self._hubspot.find_contact_by_email(contact.email)
        except Exception as exc:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                hubspot_id=None,
                message_id=message_id,
                reason=f"HubSpot lookup error: {exc}",
            )

        if existing is None:
            try:
                new_id = self._hubspot.create_contact(contact)
                return SyncResult(
                    status=SyncStatus.CREATED,
                    email=contact.email,
                    hubspot_id=new_id,
                    message_id=message_id,
                )
            except Exception as exc:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    hubspot_id=None,
                    message_id=message_id,
                    reason=f"create failed: {exc}",
                )
        else:
            hubspot_id = existing["id"]
            try:
                self._hubspot.update_contact(hubspot_id, contact, existing["properties"])
                return SyncResult(
                    status=SyncStatus.UPDATED,
                    email=contact.email,
                    hubspot_id=hubspot_id,
                    message_id=message_id,
                )
            except Exception as exc:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    hubspot_id=hubspot_id,
                    message_id=message_id,
                    reason=f"update failed: {exc}",
                )
