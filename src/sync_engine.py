import logging
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .contact_parser import SenderContact, parse_sender
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .state_manager import StateManager

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    status: str          # CREATO | AGGIORNATO | IGNORATO
    email: str
    hubspot_id: Optional[str] = None
    reason: Optional[str] = None


class SyncEngine:
    def __init__(self, config: Config):
        self._cfg = config
        self._gmail = GmailClient(config.gmail_credentials_file, config.gmail_token_file)
        self._hs = HubSpotClient(config.hubspot_api_key)
        self._state = StateManager(config.state_file)

    def run_cycle(self) -> list[SyncResult]:
        results: list[SyncResult] = []
        last_ts = self._state.get_last_timestamp()

        for message in self._gmail.list_inbox_messages(since_timestamp=last_ts):
            msg_id: str = message["id"]
            if self._state.is_processed(msg_id):
                continue

            result = self._process(message)
            results.append(result)
            self._state.mark_processed(msg_id)

        self._state.advance_timestamp()
        self._state.save()
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _process(self, message: dict) -> SyncResult:
        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(nessun oggetto)")

        sender = parse_sender(from_header)

        if sender is None:
            return SyncResult(
                status="IGNORATO",
                email=from_header or "(unknown)",
                reason="no-reply o indirizzo non valido",
            )

        if any(sender.domain.endswith(d) for d in self._cfg.exclude_domains):
            return SyncResult(
                status="IGNORATO",
                email=sender.email,
                reason=f"dominio escluso: {sender.domain}",
            )

        return self._upsert_contact(sender, subject)

    def _upsert_contact(self, sender: SenderContact, subject: str) -> SyncResult:
        existing = self._hs.find_by_email(sender.email)

        if existing:
            contact_id: str = existing["id"]
            updates = self._compute_updates(existing.get("properties", {}), sender)

            if updates:
                self._hs.update_contact(contact_id, updates)
                status = "AGGIORNATO"
            else:
                status = "IGNORATO"

            self._hs.create_email_note(contact_id, sender.email, subject)
            return SyncResult(status=status, email=sender.email, hubspot_id=contact_id)

        props = self._build_create_props(sender)
        new_contact = self._hs.create_contact(props)
        contact_id = new_contact["id"]
        self._hs.create_email_note(contact_id, sender.email, subject)
        return SyncResult(status="CREATO", email=sender.email, hubspot_id=contact_id)

    def _build_create_props(self, sender: SenderContact) -> dict:
        props: dict = {"email": sender.email, "lead_source": "Gmail"}
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company
        return props

    def _compute_updates(self, existing: dict, sender: SenderContact) -> dict:
        updates: dict = {}
        if not existing.get("firstname") and sender.first_name:
            updates["firstname"] = sender.first_name
        if not existing.get("lastname") and sender.last_name:
            updates["lastname"] = sender.last_name
        if not existing.get("company") and sender.company:
            updates["company"] = sender.company
        return updates
