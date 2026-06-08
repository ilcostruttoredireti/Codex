"""
Core sync loop: Gmail inbox → HubSpot contacts.

Status values per processed email:
  Creato     — new contact created in HubSpot
  Aggiornato — existing contact updated with missing fields
  Ignorato   — skipped (self-email, no-reply, already processed, etc.)
"""
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .contact_extractor import parse_sender
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient, HubSpotError
from .state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    status: str          # Creato | Aggiornato | Ignorato
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""     # populated when status == Ignorato


class SyncEngine:
    def __init__(self, config: Config, dry_run: bool = False):
        self._config = config
        self._dry_run = dry_run
        self._gmail = GmailClient(config.GMAIL_CREDENTIALS_PATH, config.GMAIL_TOKEN_PATH)
        self._hubspot = HubSpotClient(config.HUBSPOT_API_KEY)
        self._state = SyncState(config.STATE_FILE)
        self._own_email: Optional[str] = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_once(self) -> List[SyncResult]:
        """Process all new emails since last run. Returns one result per email."""
        self._ensure_own_email()
        message_ids, new_history_id = self._fetch_new_message_ids()

        if not message_ids:
            self._state.history_id = new_history_id
            return []

        logger.info(f"Processing {len(message_ids)} new message(s)")
        results: List[SyncResult] = []

        for msg_id in message_ids:
            result = self._process_message(msg_id)
            results.append(result)
            logger.info(
                f"[{result.status}] {result.email}"
                + (f" → HubSpot ID: {result.hubspot_id}" if result.hubspot_id else "")
                + (f" ({result.reason})" if result.reason else "")
            )

        self._state.history_id = new_history_id
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _ensure_own_email(self) -> None:
        if self._own_email is None:
            self._own_email = self._gmail.get_authenticated_email()
            logger.debug(f"Authenticated Gmail account: {self._own_email}")

    def _fetch_new_message_ids(self):
        if self._state.history_id is None:
            # First run
            if self._config.BACKFILL_DAYS > 0:
                logger.info(f"First run: backfilling last {self._config.BACKFILL_DAYS} day(s)")
                ids, hid = self._gmail.get_messages_since_days(self._config.BACKFILL_DAYS)
            else:
                logger.info("First run: saving current historyId, monitoring from now on")
                ids, hid = [], self._gmail.get_current_history_id()
            self._state.initialize(hid)
            return ids, hid

        return self._gmail.get_new_message_ids(self._state.history_id)

    def _process_message(self, msg_id: str) -> SyncResult:
        # Skip already-processed messages (happens after historyId fallback)
        if self._state.is_processed(msg_id):
            return SyncResult("Ignorato", "", reason="già processato")

        meta = self._gmail.get_message_metadata(msg_id)
        if not meta:
            return SyncResult("Ignorato", "", reason="metadata non disponibile")

        sender = parse_sender(meta["from_header"])
        if not sender:
            return SyncResult("Ignorato", "(header From non valido)", reason="header non valido")

        # Skip self-emails and no-reply addresses
        if sender.email == self._own_email:
            self._state.mark_processed(msg_id)
            return SyncResult("Ignorato", sender.email, reason="email propria")
        if GmailClient.should_skip(sender.email):
            self._state.mark_processed(msg_id)
            return SyncResult("Ignorato", sender.email, reason="indirizzo automatico")

        try:
            result = self._upsert_contact(sender, meta)
        except HubSpotError as e:
            logger.error(f"HubSpot error for {sender.email}: {e}")
            return SyncResult("Ignorato", sender.email, reason=f"errore HubSpot: {e}")

        self._state.mark_processed(msg_id)
        return result

    def _upsert_contact(self, sender, meta: dict) -> SyncResult:
        existing = None if self._dry_run else self._hubspot.find_contact_by_email(sender.email)

        if existing:
            return self._update_contact(existing, sender, meta)
        return self._create_contact(sender, meta)

    def _create_contact(self, sender, meta: dict) -> SyncResult:
        props = {"email": sender.email, "leadsource": "Gmail"}
        if sender.firstname:
            props["firstname"] = sender.firstname
        if sender.lastname:
            props["lastname"] = sender.lastname
        if sender.company:
            props["company"] = sender.company

        if self._dry_run:
            logger.info(f"[DRY RUN] Would create contact: {props}")
            return SyncResult("Creato", sender.email, hubspot_id="DRY_RUN")

        contact = self._hubspot.create_contact(props)
        contact_id = contact["id"]
        self._add_inbound_note(contact_id, sender, meta)
        return SyncResult("Creato", sender.email, hubspot_id=contact_id)

    def _update_contact(self, existing: dict, sender, meta: dict) -> SyncResult:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        updates: dict = {}

        for hs_field, value in [
            ("firstname", sender.firstname),
            ("lastname", sender.lastname),
            ("company", sender.company),
        ]:
            if value and not existing_props.get(hs_field):
                updates[hs_field] = value

        if self._dry_run:
            logger.info(f"[DRY RUN] Would update contact {contact_id}: {updates}")
            return SyncResult("Aggiornato", sender.email, hubspot_id=contact_id)

        if updates:
            self._hubspot.update_contact(contact_id, updates)
        self._add_inbound_note(contact_id, sender, meta)
        return SyncResult("Aggiornato", sender.email, hubspot_id=contact_id)

    def _add_inbound_note(self, contact_id: str, sender, meta: dict) -> None:
        body = (
            f"📧 Email ricevuta via Gmail\n"
            f"Oggetto: {meta['subject']}\n"
            f"Data: {meta['date']}\n"
            f"Da: {meta['from_header']}\n"
            f"Tag: Inbound Gmail"
        )
        try:
            self._hubspot.create_note(contact_id, body)
        except HubSpotError as e:
            logger.warning(f"Could not create note for contact {contact_id}: {e}")
