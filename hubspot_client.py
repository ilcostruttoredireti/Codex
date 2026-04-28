"""HubSpot CRM wrapper: search, create, update contacts and log activities."""

import logging
import os
import time
from typing import Optional, Tuple

from hubspot import HubSpot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

from contact_extractor import ContactData

log = logging.getLogger(__name__)

_SOURCE_LABEL = "Gmail"
_TAG_LABEL = "Inbound Gmail"


class HubSpotClient:
    def __init__(self, access_token: str = ""):
        token = access_token or os.getenv("HUBSPOT_ACCESS_TOKEN", "")
        if not token:
            raise ValueError(
                "HUBSPOT_ACCESS_TOKEN environment variable is not set.\n"
                "Create a private app in HubSpot → Settings → Integrations → Private Apps."
            )
        self.client = HubSpot(access_token=token)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[object]:
        """Return the existing SimplePublicObject or None."""
        try:
            req = PublicObjectSearchRequest(
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
                properties=["email", "firstname", "lastname", "company"],
                limit=1,
            )
            result = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            return result.results[0] if result.total > 0 else None
        except ApiException as exc:
            log.error("HubSpot search error for %s: %s", email, exc)
            return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, contact: ContactData) -> Tuple[Optional[str], str]:
        """Create a new contact. Returns (hubspot_id, status)."""
        props = self._base_properties(contact)
        props["leadsource"] = _SOURCE_LABEL

        try:
            result = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            contact_id = str(result.id)
            return contact_id, "created"
        except ApiException as exc:
            log.error("HubSpot create error for %s: %s", contact.email, exc)
            return None, "error"

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(
        self,
        contact_id: str,
        contact: ContactData,
        existing,
    ) -> Tuple[str, str]:
        """Fill in any missing fields on an existing contact."""
        existing_props: dict = (
            existing.properties if hasattr(existing, "properties") else {}
        ) or {}

        updates: dict[str, str] = {}
        if contact.first_name and not existing_props.get("firstname"):
            updates["firstname"] = contact.first_name
        if contact.last_name and not existing_props.get("lastname"):
            updates["lastname"] = contact.last_name
        if contact.company and not existing_props.get("company"):
            updates["company"] = contact.company

        if not updates:
            return contact_id, "skipped"

        try:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            return contact_id, "updated"
        except ApiException as exc:
            log.error("HubSpot update error for %s: %s", contact_id, exc)
            return contact_id, "error"

    # ------------------------------------------------------------------
    # Activity / timeline note
    # ------------------------------------------------------------------

    def add_email_activity(
        self, contact_id: str, sender_email: str, subject: str
    ) -> None:
        """Best-effort: attach a note to the contact timeline."""
        try:
            from hubspot.crm.objects.notes.models import (
                SimplePublicObjectInputForCreate as NoteCreate,
            )

            ts_ms = str(int(time.time() * 1000))
            body = (
                f"Email in entrata ricevuta\n"
                f"Da: {sender_email}\n"
                f"Oggetto: {subject or '(nessun oggetto)'}\n"
                f"Tag: {_TAG_LABEL}\n"
                f"Fonte: {_SOURCE_LABEL}"
            )

            note = self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteCreate(
                    properties={"hs_note_body": body, "hs_timestamp": ts_ms}
                )
            )

            # Associate note → contact (HubSpot defined type 202)
            self.client.crm.objects.notes.associations_api.create(
                note_id=str(note.id),
                to_object_type="contacts",
                to_object_id=str(contact_id),
                association_type="NOTE_TO_CONTACT",
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("Skipping activity note for %s: %s", contact_id, exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _base_properties(contact: ContactData) -> dict[str, str]:
        props: dict[str, str] = {"email": contact.email}
        if contact.first_name:
            props["firstname"] = contact.first_name
        if contact.last_name:
            props["lastname"] = contact.last_name
        if contact.company:
            props["company"] = contact.company
        return props
