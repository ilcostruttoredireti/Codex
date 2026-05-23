from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .config import GENERIC_EMAIL_DOMAINS, SKIP_DOMAINS, SKIP_EMAIL_PREFIXES
from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str] = None
    reason: Optional[str] = None


class ContactSyncer:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient):
        self.gmail = gmail
        self.hubspot = hubspot

    def process_message(self, message_id: str) -> SyncResult:
        sender = self.gmail.get_sender_info(message_id)
        if not sender:
            return SyncResult(SyncStatus.SKIPPED, "", reason="Mittente non leggibile")

        if self._should_skip(sender):
            return SyncResult(
                SyncStatus.SKIPPED, sender.email, reason="Indirizzo filtrato"
            )

        try:
            return self._sync_contact(sender)
        except Exception as exc:
            logger.error("Errore sync %s: %s", sender.email, exc, exc_info=True)
            return SyncResult(SyncStatus.SKIPPED, sender.email, reason=str(exc))

    # ------------------------------------------------------------------ internals

    def _should_skip(self, sender: SenderInfo) -> bool:
        if sender.domain in SKIP_DOMAINS:
            return True
        local = sender.email.split("@")[0]
        return any(local.startswith(prefix) for prefix in SKIP_EMAIL_PREFIXES)

    def _sync_contact(self, sender: SenderInfo) -> SyncResult:
        existing = self.hubspot.find_contact_by_email(sender.email)

        if existing is None:
            contact = self._create_contact(sender)
            self._log_activity(contact["id"], sender, is_new=True)
            logger.debug("Contatto creato: %s (ID %s)", sender.email, contact["id"])
            return SyncResult(SyncStatus.CREATED, sender.email, contact_id=contact["id"])

        updates = self._compute_missing_fields(existing["properties"], sender)
        if updates:
            self.hubspot.update_contact(existing["id"], updates)
            logger.debug(
                "Contatto aggiornato: %s (ID %s) — campi: %s",
                sender.email,
                existing["id"],
                list(updates.keys()),
            )

        self._log_activity(existing["id"], sender, is_new=False)
        return SyncResult(SyncStatus.UPDATED, sender.email, contact_id=existing["id"])

    def _create_contact(self, sender: SenderInfo) -> dict:
        props: dict[str, str] = {"email": sender.email, "leadsource": "EMAIL_MARKETING"}
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        company = _infer_company(sender.domain)
        if company:
            props["company"] = company
        return self.hubspot.create_contact(props)

    def _compute_missing_fields(self, existing: dict, sender: SenderInfo) -> dict:
        updates: dict[str, str] = {}
        if not existing.get("firstname") and sender.first_name:
            updates["firstname"] = sender.first_name
        if not existing.get("lastname") and sender.last_name:
            updates["lastname"] = sender.last_name
        if not existing.get("company"):
            company = _infer_company(sender.domain)
            if company:
                updates["company"] = company
        return updates

    def _log_activity(self, contact_id: str, sender: SenderInfo, is_new: bool) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        action = "Nuovo contatto creato" if is_new else "Email ricevuta da contatto esistente"
        body = (
            f"📧 Email ricevuta via Gmail [{now}]\n"
            f"Da: {sender.name} <{sender.email}>\n"
            f"Dominio: {sender.domain}\n"
            f"Azione: {action}\n"
            f"Fonte contatto: Gmail\n"
            f"Tag: Inbound Gmail"
        )
        self.hubspot.create_note(contact_id, body)


def _infer_company(domain: str) -> str:
    if domain in GENERIC_EMAIL_DOMAINS:
        return ""
    # "acmecorp.com" → "Acmecorp"
    return domain.split(".")[0].capitalize()
