"""Core sync logic: From header → HubSpot contact (create / update / ignore)."""

import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

from .extractor import extract_sender
from .hubspot_client import HubSpotClient

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


def process_sender(
    from_header: str,
    hs: HubSpotClient,
    my_email: str,
    create_notes: bool = True,
) -> SyncResult:
    """
    Process a single From header value.

    Logic:
      1. Parse → ignore if malformed / system sender
      2. Skip if it is the authenticated user's own address
      3. Search HubSpot by email
         a. Found  → fill missing fields (update) or skip (no new data)
         b. Not found → create new contact + optional note
    """
    contact = extract_sender(from_header)
    if contact is None:
        return SyncResult(SyncStatus.IGNORED, from_header, reason="ignored address")

    if contact.email == my_email.lower().strip():
        return SyncResult(SyncStatus.IGNORED, contact.email, reason="own address")

    existing = hs.find_contact_by_email(contact.email)

    if existing:
        contact_id: str = existing["id"]
        props: dict = existing.get("properties") or {}

        updates: dict = {}
        if not props.get("firstname") and contact.first_name:
            updates["firstname"] = contact.first_name
        if not props.get("lastname") and contact.last_name:
            updates["lastname"] = contact.last_name
        if not props.get("company") and contact.company:
            updates["company"] = contact.company

        if updates:
            ok = hs.update_contact(contact_id, updates)
            if ok:
                return SyncResult(SyncStatus.UPDATED, contact.email, contact_id)
            return SyncResult(SyncStatus.ERROR, contact.email, contact_id, "update failed")

        return SyncResult(SyncStatus.IGNORED, contact.email, contact_id, "no new data")

    # New contact
    contact_id = hs.create_contact(
        email=contact.email,
        first_name=contact.first_name,
        last_name=contact.last_name,
        company=contact.company,
    )
    if not contact_id:
        return SyncResult(SyncStatus.ERROR, contact.email, reason="HubSpot create failed")

    if create_notes:
        note_body = (
            "Contatto acquisito automaticamente da una email in entrata su Gmail.\n"
            "Tag: Inbound Gmail\n"
            f"Fonte: {contact.email}"
        )
        hs.create_note(contact_id, note_body)

    return SyncResult(SyncStatus.CREATED, contact.email, contact_id)
