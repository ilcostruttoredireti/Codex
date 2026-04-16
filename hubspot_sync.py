"""
HubSpot contact sync: creates or updates contacts from Gmail sender data.
Uses email as the unique key; tags every contact with "Inbound Gmail".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

from gmail_monitor import SenderContact

logger = logging.getLogger(__name__)

TAG_INBOUND = "Inbound Gmail"
SOURCE_LABEL = "Gmail"


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str]
    detail: str = ""


class HubSpotSync:
    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_contact(self, email: str) -> Optional[dict]:
        """Return the first HubSpot contact matching *email*, or None."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email,
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_analytics_source", "message"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    @staticmethod
    def _build_properties(contact: SenderContact, existing: Optional[dict] = None) -> dict[str, str]:
        """Build the property dict, filling gaps from existing record."""
        existing_props: dict = existing.properties if existing else {}

        props: dict[str, str] = {"email": contact.email}

        # Only set name/company if not already present in HubSpot
        if contact.first_name and not existing_props.get("firstname"):
            props["firstname"] = contact.first_name
        if contact.last_name and not existing_props.get("lastname"):
            props["lastname"] = contact.last_name
        if contact.company and not existing_props.get("company"):
            props["company"] = contact.company

        # Stamp the original traffic source when absent (drill-down fields are read-only)
        if not existing_props.get("hs_analytics_source"):
            props["hs_analytics_source"] = "EMAIL_MARKETING"

        return props

    def _add_tag(self, contact_id: str) -> None:
        """Record the 'Inbound Gmail' label in the contact's message/notes field."""
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties={"message": TAG_INBOUND}
                ),
            )
        except ApiException:
            pass

    def _create_timeline_activity(self, contact_id: str, subject: str) -> None:
        """Log a note on the contact timeline recording the Gmail email received."""
        try:
            note_body = f"📧 Email ricevuta via Gmail\nOggetto: {subject}\nFonte: {SOURCE_LABEL}"
            self._client.crm.objects.basic_api.create(
                object_type="notes",
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": note_body,
                        "hs_timestamp": "0",  # will be set to now by HubSpot
                    },
                    associations=[
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,  # note → contact
                                }
                            ],
                        }
                    ],
                ),
            )
        except ApiException as exc:
            logger.warning("Could not create timeline note for %s: %s", contact_id, exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self, contact: SenderContact) -> SyncResult:
        """Sync a single SenderContact to HubSpot. Returns a SyncResult."""
        existing = self._search_contact(contact.email)

        if existing:
            props = self._build_properties(contact, existing)
            # If nothing new to add, skip
            update_fields = {k: v for k, v in props.items() if k != "email"}
            if not update_fields:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    hubspot_id=existing.id,
                    detail="No new fields to update.",
                )

            try:
                self._client.crm.contacts.basic_api.update(
                    contact_id=existing.id,
                    simple_public_object_input=SimplePublicObjectInput(properties=props),
                )
                self._add_tag(existing.id)
                self._create_timeline_activity(existing.id, contact.subject)
                return SyncResult(
                    status=SyncStatus.UPDATED,
                    email=contact.email,
                    hubspot_id=existing.id,
                    detail=f"Campi aggiornati: {list(update_fields.keys())}",
                )
            except ApiException as exc:
                logger.error("Failed to update %s: %s", contact.email, exc)
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    hubspot_id=existing.id,
                    detail=f"Update error: {exc}",
                )
        else:
            props = self._build_properties(contact)
            try:
                result = self._client.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                        properties=props
                    )
                )
                new_id = result.id
                self._add_tag(new_id)
                self._create_timeline_activity(new_id, contact.subject)
                return SyncResult(
                    status=SyncStatus.CREATED,
                    email=contact.email,
                    hubspot_id=new_id,
                    detail=f"Azienda: {props.get('company', '—')}",
                )
            except ApiException as exc:
                logger.error("Failed to create %s: %s", contact.email, exc)
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    hubspot_id=None,
                    detail=f"Create error: {exc}",
                )

    def sync_batch(self, contacts: list[SenderContact]) -> list[SyncResult]:
        results = []
        seen_emails: set[str] = set()
        for c in contacts:
            if c.email in seen_emails:
                results.append(
                    SyncResult(
                        status=SyncStatus.IGNORED,
                        email=c.email,
                        hubspot_id=None,
                        detail="Duplicato nel batch corrente.",
                    )
                )
                continue
            seen_emails.add(c.email)
            results.append(self.sync(c))
        return results
