import logging
from typing import Optional

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from models import SenderInfo, SyncResult, SyncStatus

logger = logging.getLogger(__name__)


class ContactSync:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient) -> None:
        self.gmail = gmail
        self.hubspot = hubspot

    def process_message(self, message_id: str) -> Optional[SyncResult]:
        sender = self.gmail.get_message_sender(message_id)
        if not sender:
            logger.debug("Messaggio %s: mittente non analizzabile, ignorato", message_id)
            return None

        result = self._sync_contact(sender)
        self.gmail.mark_processed(message_id)
        return result

    # ── Internal ──────────────────────────────────────────────────────────────

    def _sync_contact(self, sender: SenderInfo) -> SyncResult:
        try:
            existing = self.hubspot.find_contact(sender.email)
        except Exception as exc:
            logger.error("Errore ricerca contatto %s: %s", sender.email, exc)
            return SyncResult(SyncStatus.IGNORED, sender.email, detail=str(exc))

        if existing:
            return self._update(existing, sender)
        return self._create(sender)

    def _create(self, sender: SenderInfo) -> SyncResult:
        try:
            contact = self.hubspot.create_contact(sender)
            cid = contact["id"]
            self.hubspot.create_email_note(cid, sender)
            logger.info("CREATO   %s → ID %s", sender.email, cid)
            return SyncResult(SyncStatus.CREATED, sender.email, hubspot_id=cid)
        except Exception as exc:
            logger.error("Impossibile creare contatto %s: %s", sender.email, exc)
            return SyncResult(SyncStatus.IGNORED, sender.email, detail=str(exc))

    def _update(self, existing: dict, sender: SenderInfo) -> SyncResult:
        cid = existing["id"]
        existing_props = existing.get("properties", {})
        try:
            self.hubspot.update_contact(cid, sender, existing_props)
            self.hubspot.create_email_note(cid, sender)
            logger.info("AGGIORNATO %s → ID %s", sender.email, cid)
            return SyncResult(SyncStatus.UPDATED, sender.email, hubspot_id=cid)
        except Exception as exc:
            logger.error("Impossibile aggiornare contatto %s: %s", sender.email, exc)
            return SyncResult(SyncStatus.IGNORED, sender.email, detail=str(exc))
