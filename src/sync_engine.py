"""Core Gmail → HubSpot synchronisation logic."""

import logging
from dataclasses import dataclass
from enum import Enum

from .contact_extractor import extract_contact
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str
    message_id: str
    subject: str


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        *,
        add_notes: bool = True,
    ) -> None:
        self.gmail = gmail
        self.hubspot = hubspot
        self.add_notes = add_notes

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def process_message(self, message_id: str) -> SyncResult | None:
        """
        Fetch a Gmail message, extract the sender contact, then create or
        update the matching HubSpot contact.

        Returns None when the message should be ignored (automated sender,
        unparseable header, API failure).
        """
        msg = self.gmail.get_message_sender(message_id)
        from_header = msg.get("from", "")
        subject = msg.get("subject", "(nessun oggetto)")

        contact_info = extract_contact(from_header)
        if not contact_info:
            logger.debug(f"[{message_id}] Mittente ignorato: {from_header!r}")
            return None

        email = contact_info["email"]
        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            updates = self._missing_fields(contact_info, existing.get("properties", {}))
            if updates:
                self.hubspot.update_contact(contact_id, updates)
                status = SyncStatus.UPDATED
            else:
                status = SyncStatus.IGNORED
        else:
            created = self.hubspot.create_contact(self._build_props(contact_info))
            if not created:
                logger.error(f"Impossibile creare il contatto per {email}")
                return None
            contact_id = created["id"]
            status = SyncStatus.CREATED

        if self.add_notes and status in (SyncStatus.CREATED, SyncStatus.UPDATED):
            self.hubspot.add_note(
                contact_id,
                self._note_body(from_header, subject),
            )

        return SyncResult(
            status=status,
            email=email,
            contact_id=contact_id,
            message_id=message_id,
            subject=subject,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_props(info: dict) -> dict:
        """Build the HubSpot property dict for a new contact."""
        props: dict[str, str] = {
            "email": info["email"],
            "leadsource": "EMAIL",
        }
        if info["first_name"]:
            props["firstname"] = info["first_name"]
        if info["last_name"]:
            props["lastname"] = info["last_name"]
        if info["company"]:
            props["company"] = info["company"]
        return props

    @staticmethod
    def _missing_fields(info: dict, existing_props: dict) -> dict:
        """Return only the fields absent in the existing HubSpot contact."""
        updates: dict[str, str] = {}
        if not existing_props.get("firstname") and info["first_name"]:
            updates["firstname"] = info["first_name"]
        if not existing_props.get("lastname") and info["last_name"]:
            updates["lastname"] = info["last_name"]
        if not existing_props.get("company") and info["company"]:
            updates["company"] = info["company"]
        return updates

    @staticmethod
    def _note_body(from_header: str, subject: str) -> str:
        return (
            "<b>Email ricevuta via Gmail</b><br>"
            f"Da: {from_header}<br>"
            f"Oggetto: {subject}<br>"
            "<br><em>Tag: Inbound Gmail</em>"
        )
