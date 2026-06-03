import logging
from datetime import datetime, timezone
from typing import Optional

from .config import ADD_GMAIL_LABEL, GMAIL_PROCESSED_LABEL, CREATE_EMAIL_ENGAGEMENT, SKIP_LOCAL_PARTS
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .models import SenderInfo, SyncResult
from .state import StateManager

logger = logging.getLogger(__name__)


class GmailHubSpotSync:
    """Coordina il flusso Gmail → HubSpot."""

    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: StateManager,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.state = state

    # ──────────────────────────────────────────────
    # Ciclo principale
    # ──────────────────────────────────────────────

    def run_once(self) -> list[SyncResult]:
        last_sync = self.state.get_last_sync_time()
        now = datetime.now(timezone.utc)

        raw_messages = self.gmail.get_new_messages(since=last_sync)
        logger.info(f"Trovati {len(raw_messages)} messaggi da controllare.")

        results: list[SyncResult] = []
        for msg in raw_messages:
            message_id = msg["id"]

            if self.state.is_processed(message_id):
                logger.debug(f"Già processato, skip: {message_id}")
                continue

            details = self.gmail.get_message_details(message_id)
            if not details:
                continue

            result = self._process_message(details)
            results.append(result)

            self.state.mark_processed(
                message_id, result.email, result.status, result.hubspot_id
            )

            if ADD_GMAIL_LABEL and result.status in ("CREATO", "AGGIORNATO"):
                self._apply_processed_label(message_id)

        self.state.update_last_sync_time(now)
        return results

    # ──────────────────────────────────────────────
    # Elaborazione singola email
    # ──────────────────────────────────────────────

    def _process_message(self, message: dict) -> SyncResult:
        message_id = message["id"]

        sender = self.gmail.parse_sender(message)
        if not sender:
            return SyncResult("IGNORATO", "sconosciuto", None, message_id, "impossibile parsare il mittente")

        if self._is_automated_sender(sender):
            return SyncResult("IGNORATO", sender.email, None, message_id, "mittente automatico")

        existing = self.hubspot.find_contact_by_email(sender.email)

        if existing:
            contact_id = existing["id"]
            self.hubspot.update_contact_missing_fields(contact_id, sender, existing)
            if CREATE_EMAIL_ENGAGEMENT:
                self.hubspot.create_email_engagement(contact_id, sender)
            logger.info(f"AGGIORNATO: {sender.email} (HubSpot ID: {contact_id})")
            return SyncResult("AGGIORNATO", sender.email, contact_id, message_id)
        else:
            contact_id = self.hubspot.create_contact(sender)
            if not contact_id:
                return SyncResult(
                    "IGNORATO", sender.email, None, message_id, "errore creazione contatto HubSpot"
                )
            if CREATE_EMAIL_ENGAGEMENT:
                self.hubspot.create_email_engagement(contact_id, sender)
            self.hubspot.add_tag_note(contact_id, sender)
            logger.info(f"CREATO: {sender.email} (HubSpot ID: {contact_id})")
            return SyncResult("CREATO", sender.email, contact_id, message_id)

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    def _is_automated_sender(self, sender: SenderInfo) -> bool:
        local = sender.email.split("@")[0].lower()
        # Match esatto o sottostringa per pattern noreply/no-reply
        if local in SKIP_LOCAL_PARTS:
            return True
        if any(p in local for p in ("noreply", "no-reply", "donotreply", "bounce", "autorespond")):
            return True
        return False

    def _apply_processed_label(self, message_id: str):
        label_id = self.gmail.get_or_create_label(GMAIL_PROCESSED_LABEL)
        if label_id:
            self.gmail.add_label_to_message(message_id, label_id)
