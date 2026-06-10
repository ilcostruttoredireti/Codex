"""Core sync logic: Gmail message → HubSpot contact."""

import dataclasses
import logging
from enum import Enum
from typing import Optional

from config import CONTACT_SOURCE, CONTACT_TAG, IGNORED_DOMAINS
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state import SyncState
from utils import (
    domain_to_company,
    extract_domain,
    is_ignorable,
    parse_sender,
    split_name,
)

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclasses.dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str] = None
    reason: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}]", f"email={self.email}"]
        if self.contact_id:
            parts.append(f"id={self.contact_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return "  ".join(parts)


class GmailHubSpotSyncer:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: SyncState,
        processed_label_id: Optional[str] = None,
        log_activity: bool = True,
    ):
        self._gmail = gmail
        self._hs = hubspot
        self._state = state
        self._label_id = processed_label_id
        self._log_activity = log_activity

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def initial_scan(self, max_messages: int = 200) -> list[SyncResult]:
        """Process recent INBOX messages on the very first run."""
        logger.info("Running initial inbox scan (up to %d messages)…", max_messages)
        results = []
        for stub in self._gmail.list_inbox_messages(max_results=max_messages):
            msg_id = stub["id"]
            if self._state.is_processed(msg_id):
                continue
            result = self._process_message(msg_id)
            results.append(result)
        # Anchor history ID so future runs are incremental
        self._state.history_id = self._gmail.get_current_history_id()
        return results

    def incremental_sync(self) -> list[SyncResult]:
        """Process only messages received since the last run."""
        if not self._state.history_id:
            return self.initial_scan()

        new_ids, latest_id = self._gmail.get_new_message_ids(self._state.history_id)

        results = []
        for msg_id in new_ids:
            if self._state.is_processed(msg_id):
                continue
            result = self._process_message(msg_id)
            results.append(result)

        self._state.history_id = latest_id
        return results

    # ------------------------------------------------------------------
    # Per-message processing
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult:
        raw_from = self._gmail.get_message_sender(message_id)
        if not raw_from:
            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.IGNORED, "", reason="no From header")

        display_name, email = parse_sender(raw_from)

        if is_ignorable(email, IGNORED_DOMAINS):
            self._state.mark_processed(message_id)
            return SyncResult(SyncStatus.IGNORED, email, reason="ignorable sender")

        result = self._upsert_contact(email, display_name, message_id)
        self._state.mark_processed(message_id)

        if self._label_id and result.status != SyncStatus.IGNORED:
            self._gmail.apply_label(message_id, self._label_id)

        return result

    def _upsert_contact(
        self, email: str, display_name: Optional[str], message_id: str
    ) -> SyncResult:
        firstname, lastname = split_name(display_name)
        domain = extract_domain(email)
        company = domain_to_company(domain)

        existing = self._hs.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            props = existing.get("properties", {})
            updates: dict[str, str] = {}

            # Fill only missing fields — never overwrite existing data
            if firstname and not props.get("firstname"):
                updates["firstname"] = firstname
            if lastname and not props.get("lastname"):
                updates["lastname"] = lastname
            if company and not props.get("company"):
                updates["company"] = company
            if not props.get("leadsource"):
                updates["leadsource"] = CONTACT_SOURCE

            if updates:
                self._hs.update_contact(contact_id, updates)
                status = SyncStatus.UPDATED
            else:
                status = SyncStatus.IGNORED

            if self._log_activity:
                self._hs.log_email_activity(
                    contact_id,
                    subject="Inbound Gmail",
                    body=f"Email ricevuta da {display_name or email} ({email}).\nTag: {CONTACT_TAG}",
                )

            return SyncResult(status, email, contact_id)

        # New contact
        contact_id = self._hs.create_contact(
            email=email,
            firstname=firstname,
            lastname=lastname,
            company=company or "",
            source=CONTACT_SOURCE,
            notes=f"Tag: {CONTACT_TAG}",
        )
        if not contact_id:
            return SyncResult(SyncStatus.IGNORED, email, reason="HubSpot create failed")

        if self._log_activity:
            self._hs.log_email_activity(
                contact_id,
                subject="Inbound Gmail",
                body=f"Nuovo contatto creato da email in arrivo: {display_name or email} ({email}).\nTag: {CONTACT_TAG}",
            )

        return SyncResult(SyncStatus.CREATED, email, contact_id)
