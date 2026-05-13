import logging
import time
from typing import Optional

from gmail_client import GmailClient
from hubspot_client import HubSpotClient, _CONTACT_TAG
from models import SyncResult, SyncStatus

logger = logging.getLogger(__name__)


class GmailHubSpotSync:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient, poll_interval: int = 60):
        self._gmail = gmail
        self._hubspot = hubspot
        self._poll_interval = poll_interval

    def process_message(self, msg_id: str) -> SyncResult:
        headers = self._gmail.get_message_headers(msg_id)
        if not headers:
            return SyncResult(SyncStatus.ERROR, reason="Could not fetch headers")

        from_header = headers.get("From", "")
        subject = headers.get("Subject", "")

        if not from_header:
            return SyncResult(SyncStatus.IGNORED, reason="Missing From header")

        sender = GmailClient.parse_sender(from_header)
        if sender is None:
            return SyncResult(SyncStatus.IGNORED, reason="System/unparseable sender")

        existing = self._hubspot.find_contact(sender.email)

        if existing:
            contact_id = existing["id"]
            self._hubspot.update_contact(contact_id, sender, existing)
            self._hubspot.log_email_activity(contact_id, subject, sender.email)
            self._maybe_add_tag_note(contact_id, sender.email)
            return SyncResult(SyncStatus.UPDATED, email=sender.email, contact_id=contact_id)

        new_contact = self._hubspot.create_contact(sender)
        if new_contact is None:
            return SyncResult(SyncStatus.ERROR, email=sender.email, reason="HubSpot create failed")

        contact_id = new_contact["id"]
        self._hubspot.log_email_activity(contact_id, subject, sender.email)
        self._maybe_add_tag_note(contact_id, sender.email)
        return SyncResult(SyncStatus.CREATED, email=sender.email, contact_id=contact_id)

    def _maybe_add_tag_note(self, contact_id: str, email: str) -> None:
        self._hubspot.add_note(contact_id, f"Tag: {_CONTACT_TAG} | source email: {email}")

    def run_once(self) -> list[SyncResult]:
        messages = self._gmail.fetch_unsynced_messages()
        results: list[SyncResult] = []

        if not messages:
            logger.debug("No new messages to process")
            return results

        logger.info("Processing %d messages", len(messages))
        for msg in messages:
            msg_id = msg["id"]
            result = self.process_message(msg_id)
            self._gmail.mark_as_synced(msg_id)
            results.append(result)
            print(str(result))

        return results

    def run_forever(self) -> None:
        logger.info("Gmail → HubSpot sync started (interval=%ds)", self._poll_interval)
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Sync stopped")
                break
            except Exception:
                logger.exception("Unexpected error in sync loop")
            time.sleep(self._poll_interval)
