"""Motore di sincronizzazione: coordina Gmail → HubSpot."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot

from gmail_client import get_gmail_service, fetch_new_messages, get_profile_history_id
from hubspot_client import (
    get_hubspot_client,
    find_contact_by_email,
    create_contact,
    update_contact,
    add_timeline_note,
)
from state import SyncState

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str] = None
    reason: Optional[str] = None

    def __str__(self):
        parts = [f"[{self.status.value}] {self.email}"]
        if self.contact_id:
            parts.append(f"ID HubSpot: {self.contact_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


class GmailHubSpotSyncer:
    def __init__(self, config: dict):
        self.config = config
        self.state = SyncState(config["state_file"])
        self.gmail = get_gmail_service()
        self.hs_client: hubspot.Client = get_hubspot_client(config["hubspot_access_token"])

    def run_once(self) -> list[SyncResult]:
        """Esegue un singolo ciclo di sincronizzazione."""
        history_id = self.state.history_id

        # Primo avvio: salva l'history_id attuale senza processare vecchie email
        if not history_id:
            logger.info("Primo avvio: salvo history ID corrente, monitoro solo email future.")
            current_id = get_profile_history_id(self.gmail)
            self.state.history_id = current_id
            return []

        messages = fetch_new_messages(self.gmail, since_history_id=history_id)
        results = []

        for msg in messages:
            if self.state.is_processed(msg["message_id"]):
                continue

            result = self._process_message(msg)
            results.append(result)

            # Aggiorna history_id al più recente processato
            self.state.mark_processed(
                msg["message_id"],
                new_history_id=msg.get("history_id") or history_id,
            )

            self._log_result(result)

        return results

    def _process_message(self, msg: dict) -> SyncResult:
        sender = msg["sender"]
        email = sender["email"]

        if self._should_ignore(sender):
            return SyncResult(SyncStatus.IGNORED, email, reason="dominio/email escluso")

        try:
            existing = find_contact_by_email(self.hs_client, email)

            if existing:
                update_contact(self.hs_client, existing["id"], sender, existing["properties"])
                if self.config.get("add_timeline_note"):
                    add_timeline_note(
                        self.hs_client, existing["id"],
                        msg["subject"], msg["date"], email
                    )
                return SyncResult(SyncStatus.UPDATED, email, contact_id=existing["id"])
            else:
                new_contact = create_contact(self.hs_client, sender)
                contact_id = new_contact["id"] if new_contact else None
                if contact_id and self.config.get("add_timeline_note"):
                    add_timeline_note(
                        self.hs_client, contact_id,
                        msg["subject"], msg["date"], email
                    )
                return SyncResult(SyncStatus.CREATED, email, contact_id=contact_id)

        except Exception as e:
            logger.exception("Errore processando %s", email)
            return SyncResult(SyncStatus.ERROR, email, reason=str(e))

    def _should_ignore(self, sender: dict) -> bool:
        """Restituisce True se il mittente va ignorato."""
        email = sender["email"]
        domain = sender.get("domain", "")

        if email in self.config["ignore_emails"]:
            return True
        if domain in self.config["ignore_domains"]:
            return True
        # Ignora indirizzi no-reply generici
        local_part = email.split("@")[0] if "@" in email else email
        if any(kw in local_part for kw in ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster")):
            return True

        return False

    def _log_result(self, result: SyncResult):
        if result.status == SyncStatus.ERROR:
            logger.error(str(result))
        elif result.status == SyncStatus.IGNORED:
            logger.debug(str(result))
        else:
            logger.info(str(result))
