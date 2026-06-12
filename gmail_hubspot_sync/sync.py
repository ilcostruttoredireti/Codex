import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional

from .contact_parser import extract_contact_from_email
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state import StateManager

logger = logging.getLogger(__name__)


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    message_id: str


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: StateManager,
        own_email: Optional[str] = None,
        initial_messages: int = 50,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state = state
        self._own_email = own_email.lower() if own_email else None
        self._initial_messages = initial_messages

    # ── Singolo messaggio ─────────────────────────────────────────────────────

    def process_message(self, message_id: str) -> SyncResult:
        """Elabora un messaggio Gmail e sincronizza il mittente in HubSpot."""

        if self._state.is_processed(message_id):
            return SyncResult(SyncStatus.SKIPPED, "", None, message_id)

        headers = self._gmail.get_message_headers(message_id)
        if not headers or not headers.get("from"):
            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.SKIPPED, "", None, message_id)

        contact = extract_contact_from_email(headers["from"])

        # Salta email malformate o proprie (self-sent)
        if not contact.email or "@" not in contact.email:
            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.SKIPPED, contact.email or "", None, message_id)

        if self._own_email and contact.email == self._own_email:
            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.SKIPPED, contact.email, None, message_id)

        # ── Cerca contatto esistente in HubSpot ───────────────────────────────
        existing = self._hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id = existing["id"]
            props = existing.get("properties") or {}

            # Aggiorna solo i campi vuoti (non sovrascrivere dati già presenti)
            updates: dict = {}
            if contact.first_name and not props.get("firstname"):
                updates["firstname"] = contact.first_name
            if contact.last_name and not props.get("lastname"):
                updates["lastname"] = contact.last_name
            if contact.company and not props.get("company"):
                updates["company"] = contact.company
            if not props.get("lead_source"):
                updates["lead_source"] = "Gmail"

            if updates:
                self._hubspot.update_contact(contact_id, updates)

            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.UPDATED, contact.email, contact_id, message_id)

        # ── Crea nuovo contatto ───────────────────────────────────────────────
        result = self._hubspot.create_contact(
            email=contact.email,
            first_name=contact.first_name,
            last_name=contact.last_name,
            company=contact.company,
        )

        if result:
            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.CREATED, contact.email, result["id"], message_id)

        return SyncResult(SyncStatus.ERROR, contact.email, None, message_id)

    # ── Ciclo di sync ─────────────────────────────────────────────────────────

    def run_sync_cycle(self) -> List[SyncResult]:
        """
        Recupera i nuovi messaggi Gmail e li sincronizza in HubSpot.
        Usa l'API History per sync incrementale; al primo avvio carica le ultime N email.
        """
        if self._state.last_history_id is None:
            logger.info(
                f"Primo avvio: recupero ultimi {self._initial_messages} messaggi inbox..."
            )
            message_ids, history_id = self._gmail.get_recent_inbox_messages(
                max_results=self._initial_messages
            )
            self._state.last_history_id = history_id
        else:
            message_ids, history_id = self._gmail.get_messages_since_history(
                self._state.last_history_id
            )
            if message_ids is None:
                # History ID scaduto: fallback ai messaggi recenti
                logger.warning("History ID scaduto — recupero messaggi recenti")
                message_ids, history_id = self._gmail.get_recent_inbox_messages(
                    max_results=self._initial_messages
                )
            self._state.last_history_id = history_id

        logger.info(f"{len(message_ids)} messaggio/i da processare")

        results: List[SyncResult] = []
        for msg_id in message_ids:
            res = self.process_message(msg_id)
            results.append(res)
            if res.status != SyncStatus.SKIPPED:
                logger.info(
                    f"  [{res.status.value:>10}]  {res.email:<40}  ID: {res.contact_id or '—'}"
                )

        self._state.save()
        return results
