"""HubSpot API client — search, create, and update contacts."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicObject

from . import config
from .gmail_client import SenderInfo

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str = ""
    reason: str = ""

    def __str__(self) -> str:
        parts = [
            f"Stato: {self.status.value}",
            f"Email: {self.email}",
            f"ID HubSpot: {self.contact_id or 'N/A'}",
        ]
        if self.reason:
            parts.append(f"Motivo: {self.reason}")
        return " | ".join(parts)


class HubSpotClient:
    def __init__(self) -> None:
        if not config.HUBSPOT_ACCESS_TOKEN:
            raise ValueError(
                "HUBSPOT_ACCESS_TOKEN is not set. "
                "Create a HubSpot Private App and set the token in .env."
            )
        self._client = hubspot.Client.create(
            access_token=config.HUBSPOT_ACCESS_TOKEN
        )

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[SimplePublicObject]:
        """Return the HubSpot contact with this email, or None if not found."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email.lower(),
                        )
                    ]
                )
            ],
            properties=[
                "email",
                "firstname",
                "lastname",
                "company",
                "hs_lead_status",
                "lifecyclestage",
            ],
            limit=1,
        )

        try:
            result = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if result.total > 0:
                return result.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)

        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, info: SenderInfo) -> SyncResult:
        """Create a new HubSpot contact from *info*."""
        properties = _build_properties(info, is_new=True)
        payload = SimplePublicObjectInputForCreate(properties=properties)

        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=payload
            )
            logger.info("Created contact %s (id=%s).", info.email, contact.id)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=info.email,
                contact_id=contact.id,
            )
        except ApiException as exc:
            # 409 = contact already exists (race condition) — treat as ignored
            if exc.status == 409:
                logger.warning(
                    "Contact %s already exists (conflict on create).", info.email
                )
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=info.email,
                    reason="Conflitto: contatto già presente",
                )
            logger.error("Error creating contact %s: %s", info.email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=info.email,
                reason=f"Errore API: {exc.status}",
            )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(
        self, contact: SimplePublicObject, info: SenderInfo
    ) -> SyncResult:
        """Fill in missing fields on an existing HubSpot contact."""
        existing = contact.properties or {}
        updates = _build_update_properties(existing, info)

        if not updates:
            logger.debug("No fields to update for %s.", info.email)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=info.email,
                contact_id=contact.id,
                reason="Nessun campo da aggiornare",
            )

        try:
            from hubspot.crm.contacts import SimplePublicObjectInput

            self._client.crm.contacts.basic_api.update(
                contact_id=contact.id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=updates
                ),
            )
            logger.info(
                "Updated contact %s (id=%s) — fields: %s.",
                info.email,
                contact.id,
                list(updates.keys()),
            )
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=info.email,
                contact_id=contact.id,
            )
        except ApiException as exc:
            logger.error("Error updating contact %s: %s", info.email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=info.email,
                contact_id=contact.id,
                reason=f"Errore aggiornamento: {exc.status}",
            )

    # ------------------------------------------------------------------
    # Timeline activity (optional)
    # ------------------------------------------------------------------

    def log_email_activity(self, contact_id: str, info: SenderInfo) -> None:
        """
        Create a note on the contact timeline recording the inbound email.
        Uses the CRM notes API (engagements v3 / simple object 'notes').
        """
        note_body = (
            f"Email ricevuta da {info.full_name or info.email}.\n"
            f"Oggetto: {info.subject or '(nessuno)'}\n"
            f"Fonte: Gmail (sync automatico)"
        )
        try:
            from hubspot.crm.objects.notes import (
                ApiException as NotesApiException,
                SimplePublicObjectInputForCreate as NoteCreate,
            )
            from hubspot.crm.associations.v4 import (
                AssociationSpec,
            )

            note = self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteCreate(
                    properties={
                        "hs_note_body": note_body,
                        "hs_timestamp": _now_ms(),
                    }
                )
            )

            # Associate note → contact
            self._client.crm.objects.notes.associations_api.create(
                note_id=note.id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_type="note_to_contact",
            )
            logger.debug("Logged timeline note for contact %s.", contact_id)
        except Exception as exc:
            # Timeline logging is optional — don't let it break the sync
            logger.warning("Could not log timeline activity: %s", exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _now_ms() -> str:
    """Current UTC timestamp in milliseconds (as string for HubSpot)."""
    import time
    return str(int(time.time() * 1000))


def _build_properties(info: SenderInfo, *, is_new: bool) -> dict[str, str]:
    props: dict[str, str] = {"email": info.email}

    if info.first_name:
        props["firstname"] = info.first_name
    if info.last_name:
        props["lastname"] = info.last_name
    if info.company:
        props["company"] = info.company

    props["lead_source"] = config.CONTACT_SOURCE_LABEL
    # hs_analytics_source maps to the "Original source" property
    props["hs_analytics_source"] = "OTHER"

    return props


def _build_update_properties(
    existing: dict, info: SenderInfo
) -> dict[str, str]:
    """Return only the fields that are blank in HubSpot and available in *info*."""
    updates: dict[str, str] = {}

    def _missing(key: str) -> bool:
        return not existing.get(key, "").strip()

    if _missing("firstname") and info.first_name:
        updates["firstname"] = info.first_name
    if _missing("lastname") and info.last_name:
        updates["lastname"] = info.last_name
    if _missing("company") and info.company:
        updates["company"] = info.company
    if _missing("lead_source"):
        updates["lead_source"] = config.CONTACT_SOURCE_LABEL

    return updates
