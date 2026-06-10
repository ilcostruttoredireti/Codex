"""Core synchronisation logic: fetch Gmail messages and upsert HubSpot contacts."""

import logging
from dataclasses import dataclass, field
from typing import Optional

from contact_parser import is_automated_sender, parse_sender
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import StateManager

logger = logging.getLogger(__name__)

STATUS_CREATED = "Creato"
STATUS_UPDATED = "Aggiornato"
STATUS_SKIPPED = "Ignorato"


@dataclass
class SyncResult:
    status: str
    email: str
    hubspot_id: Optional[str] = None
    reason: str = ""


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: StateManager,
        max_per_cycle: int = 50,
        enable_notes: bool = True,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state = state
        self._max_per_cycle = max_per_cycle
        self._enable_notes = enable_notes

    # ── public interface ──────────────────────────────────────────────────────

    def run_cycle(self) -> list:
        """
        Fetch new inbox messages, sync their senders to HubSpot, and return a
        SyncResult for every email processed this cycle.
        """
        messages = self._gmail.get_messages_since(
            since=self._state.last_check,
            max_results=self._max_per_cycle,
        )

        results = []
        for msg in messages:
            if self._state.is_processed(msg["id"]):
                continue
            result = self._process(msg)
            results.append(result)
            self._state.mark_processed(msg["id"])

        self._state.update_last_check()
        return results

    # ── private helpers ───────────────────────────────────────────────────────

    def _process(self, msg: dict) -> SyncResult:
        contact = parse_sender(msg["raw_from"])

        if not contact.get("email"):
            return SyncResult(
                status=STATUS_SKIPPED,
                email=msg.get("raw_from", "unknown"),
                reason="Could not parse sender email",
            )

        email = contact["email"]

        if is_automated_sender(email):
            return SyncResult(
                status=STATUS_SKIPPED,
                email=email,
                reason="Automated/system sender filtered out",
            )

        existing = self._hubspot.find_contact_by_email(email)

        if existing:
            return self._update(existing, contact, msg)
        return self._create(contact, msg)

    def _create(self, contact: dict, msg: dict) -> SyncResult:
        props = self._build_props(contact)
        created = self._hubspot.create_contact(props)

        if not created:
            return SyncResult(
                status=STATUS_SKIPPED,
                email=contact["email"],
                reason="HubSpot create call failed",
            )

        if self._enable_notes:
            self._hubspot.create_note(
                created.id,
                self._note_body(msg, contact),
            )

        return SyncResult(
            status=STATUS_CREATED,
            email=contact["email"],
            hubspot_id=created.id,
        )

    def _update(self, existing, contact: dict, msg: dict) -> SyncResult:
        existing_props = existing.properties or {}
        update_props = {}

        for key in ("firstname", "lastname", "company"):
            if contact.get(key) and not existing_props.get(key):
                update_props[key] = contact[key]

        if update_props:
            self._hubspot.update_contact(existing.id, update_props)

        if self._enable_notes:
            self._hubspot.create_note(
                existing.id,
                self._note_body(msg, contact),
            )

        return SyncResult(
            status=STATUS_UPDATED,
            email=contact["email"],
            hubspot_id=existing.id,
        )

    @staticmethod
    def _build_props(contact: dict) -> dict:
        props = {"email": contact["email"]}
        for key in ("firstname", "lastname", "company"):
            if contact.get(key):
                props[key] = contact[key]
        props["leadsource"] = "OTHER"
        return props

    @staticmethod
    def _note_body(msg: dict, contact: dict) -> str:
        lines = [
            "Email ricevuta via Gmail",
            f"Da: {contact['email']}",
            f"Oggetto: {msg.get('subject', '')}",
            f"Data: {msg.get('date', '')}",
            "Tag: Inbound Gmail",
        ]
        return "\n".join(lines)
