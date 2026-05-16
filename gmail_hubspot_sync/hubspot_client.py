"""HubSpot CRM API wrapper for contact upsert and timeline events."""

import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicUpsertObject

logger = logging.getLogger(__name__)

# HubSpot property for "Inbound Gmail" tag stored as a custom property.
# If you want a real list membership instead, use the Lists API.
TAG_PROPERTY = "hs_lead_status"  # placeholder; adjust to your custom property name


class ContactResult:
    def __init__(self, status: str, email: str, contact_id: str) -> None:
        self.status = status      # "created" | "updated" | "skipped"
        self.email = email
        self.contact_id = contact_id

    def __repr__(self) -> str:
        return f"ContactResult(status={self.status!r}, email={self.email!r}, id={self.contact_id!r})"


class HubSpotClient:
    def __init__(self, token: str) -> None:
        self._client = hubspot.Client.create(access_token=token)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return existing HubSpot contact or None."""
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
            properties=["email", "firstname", "lastname", "company", "leadsource"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.results:
                return response.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    def create_contact(self, props: dict) -> Optional[str]:
        """Create a new contact. Returns the new contact ID or None on failure."""
        try:
            response = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return str(response.id)
        except ApiException as exc:
            logger.error("HubSpot create error: %s", exc)
            return None

    def update_contact(self, contact_id: str, props: dict) -> bool:
        """Patch an existing contact with only the provided properties. Returns success."""
        from hubspot.crm.contacts import SimplePublicObjectInput

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            return True
        except ApiException as exc:
            logger.error("HubSpot update error for %s: %s", contact_id, exc)
            return False

    def add_timeline_event(self, contact_id: str, email_address: str, subject: str) -> None:
        """Create a note on the contact timeline describing the received email."""
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteInput,
        )
        from hubspot.crm.associations.v4.models import AssociationSpec, AssociationSpecAssociationCategory

        note_body = f"Inbound Gmail received from {email_address}.\nSubject: {subject}"
        try:
            note = self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteInput(
                    properties={
                        "hs_note_body": note_body,
                        "hs_timestamp": str(int(__import__("time").time() * 1000)),
                    }
                )
            )
            # Associate note → contact (type 202 = note to contact)
            self._client.crm.associations.v4.basic_api.create(
                object_type="notes",
                object_id=note.id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_spec=[
                    AssociationSpec(
                        association_category=AssociationSpecAssociationCategory.HUBSPOT_DEFINED,
                        association_type_id=202,
                    )
                ],
            )
        except Exception as exc:
            logger.warning("Could not create timeline note for contact %s: %s", contact_id, exc)
