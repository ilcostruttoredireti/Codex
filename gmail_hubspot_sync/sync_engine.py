"""
Core sync logic: for each new Gmail message, create or update the sender
contact in HubSpot, log an activity note, and return a result record.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from config import Config
from contact_extractor import extract_contact
from forward_parser import extract_forwarded_sender, is_forwarded
from gmail_client import GmailClient, GmailMessage
from hubspot_client import HubSpotClient
from state_manager import StateManager

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: str
    message_id: str
    reason: str = ""

    def __str__(self) -> str:
        base = f"[{self.status}] {self.email} → HubSpot ID: {self.hubspot_id or '-'}"
        if self.reason:
            base += f" ({self.reason})"
        return base


class SyncEngine:
    def __init__(self, config: Config, gmail: GmailClient, hubspot: HubSpotClient, state: StateManager):
        self._cfg = config
        self._gmail = gmail
        self._hs = hubspot
        self._state = state

    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        """Fetch new Gmail messages and sync contacts. Returns all results."""
        since = self._state.get_last_sync()
        now = datetime.now(tz=timezone.utc)

        logger.info("Fetching emails since %s …", since or "beginning")
        messages = self._gmail.get_new_messages(since=since)
        logger.info("Found %d message(s).", len(messages))

        results: list[SyncResult] = []
        for msg in messages:
            if self._state.is_processed(msg.message_id):
                continue
            result = self._process_message(msg)
            results.append(result)
            self._state.mark_processed(msg.message_id)

        self._state.set_last_sync(now)
        return results

    # ------------------------------------------------------------------

    def _process_message(self, msg: GmailMessage) -> SyncResult:
        # For forwarded emails, prefer the original sender over the forwarder
        if is_forwarded(msg.subject) and msg.plaintext_body:
            parsed = extract_forwarded_sender(msg.plaintext_body)
            if parsed:
                name, email = parsed
                sender_str = f"{name} <{email}>" if name else email
                contact_info = extract_contact(sender_str)
            else:
                contact_info = extract_contact(msg.sender)
        else:
            contact_info = extract_contact(msg.sender)

        # Skip unresolvable or internal senders
        if contact_info is None:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=msg.sender,
                hubspot_id="",
                message_id=msg.message_id,
                reason="impossibile estrarre email",
            )

        email = contact_info.email

        # Skip no-reply / system addresses
        if self._should_skip(email):
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                hubspot_id="",
                message_id=msg.message_id,
                reason="indirizzo di sistema ignorato",
            )

        try:
            return self._sync_contact(contact_info, msg)
        except Exception as exc:
            logger.error("Error syncing contact %s: %s", email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                hubspot_id="",
                message_id=msg.message_id,
                reason=f"errore: {exc}",
            )

    def _sync_contact(self, contact_info, msg: GmailMessage) -> SyncResult:
        from contact_extractor import ContactInfo

        email = contact_info.email
        existing = self._hs.find_contact_by_email(email)
        received_at = msg.date.strftime("%Y-%m-%dT%H:%M:%S.000Z")

        if existing:
            # Update only missing fields
            updates = self._build_updates(existing, contact_info)
            if updates:
                self._hs.update_contact(existing.id, updates)
            self._hs.log_email_activity(existing.id, msg.subject, msg.snippet, received_at)
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=email,
                hubspot_id=existing.id,
                message_id=msg.message_id,
            )
        else:
            props = self._build_create_props(contact_info)
            new_contact = self._hs.create_contact(props)
            self._hs.log_email_activity(new_contact.id, msg.subject, msg.snippet, received_at)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=email,
                hubspot_id=new_contact.id,
                message_id=msg.message_id,
            )

    # ------------------------------------------------------------------

    def _build_create_props(self, info) -> dict[str, str]:
        props: dict[str, str] = {
            "email": info.email,
            "hs_lead_source": self._cfg.CONTACT_SOURCE_LABEL,
        }
        if info.first_name:
            props["firstname"] = info.first_name
        if info.last_name:
            props["lastname"] = info.last_name
        if info.company:
            props["company"] = info.company
        return props

    def _build_updates(self, existing, info) -> dict[str, str]:
        updates: dict[str, str] = {}
        if not existing.first_name and info.first_name:
            updates["firstname"] = info.first_name
        if not existing.last_name and info.last_name:
            updates["lastname"] = info.last_name
        if not existing.company and info.company:
            updates["company"] = info.company
        return updates

    def _should_skip(self, email: str) -> bool:
        domain = email.split("@")[-1] if "@" in email else ""
        if domain in self._cfg.SKIP_DOMAINS:
            return True
        return any(email.startswith(p) for p in self._cfg.SKIP_PREFIXES)
