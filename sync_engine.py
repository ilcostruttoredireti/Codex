"""Core sync logic: one Gmail message → one HubSpot contact upsert."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

from contact_parser import ContactInfo, parse_from_header
from gmail_reader import GmailReader
from hubspot_writer import HubSpotWriter

logger = logging.getLogger(__name__)


class Status(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: Status
    email: str
    contact_id: Optional[str] = None
    reason: Optional[str] = None


class SyncEngine:
    def __init__(
        self,
        gmail: GmailReader,
        hubspot: HubSpotWriter,
        create_notes: bool = True,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.create_notes = create_notes

    def process_message(self, message: Dict) -> SyncResult:
        """Process a single Gmail message dict and sync its sender to HubSpot."""
        from_header = GmailReader.get_header(message, "From")
        subject = GmailReader.get_header(message, "Subject") or "(nessun oggetto)"

        if not from_header:
            return SyncResult(Status.IGNORED, email="", reason="From header assente")

        contact = parse_from_header(from_header)
        if not contact:
            return SyncResult(Status.IGNORED, email=from_header, reason="Mittente filtrato o non analizzabile")

        existing = self.hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id = existing["id"]
            updates = _build_updates(existing.get("properties", {}), contact)
            if not updates:
                return SyncResult(Status.IGNORED, contact.email, contact_id, reason="Nessun dato nuovo")
            ok = self.hubspot.update_contact(contact_id, updates)
            return SyncResult(Status.UPDATED if ok else Status.ERROR, contact.email, contact_id)

        # New contact
        props = _build_create_props(contact)
        contact_id = self.hubspot.create_contact(props)
        if not contact_id:
            return SyncResult(Status.ERROR, contact.email)

        if self.create_notes:
            note = (
                f"📧 Email inbound da Gmail\n"
                f"Mittente: {from_header}\n"
                f"Oggetto: {subject}\n"
                f"Tag: Inbound Gmail | Fonte: Gmail"
            )
            self.hubspot.create_note(contact_id, note)

        return SyncResult(Status.CREATED, contact.email, contact_id)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_create_props(contact: ContactInfo) -> Dict[str, str]:
    props: Dict[str, str] = {"email": contact.email, "lifecyclestage": "lead"}
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    return props


def _build_updates(existing_props: Dict, contact: ContactInfo) -> Dict[str, str]:
    """Return only the fields that are missing from the existing contact."""
    updates: Dict[str, str] = {}
    if contact.first_name and not existing_props.get("firstname"):
        updates["firstname"] = contact.first_name
    if contact.last_name and not existing_props.get("lastname"):
        updates["lastname"] = contact.last_name
    if contact.company and not existing_props.get("company"):
        updates["company"] = contact.company
    return updates
