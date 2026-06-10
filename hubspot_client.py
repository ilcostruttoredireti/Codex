"""HubSpot CRM client — contact search, create, update and timeline notes."""

import logging
import time
from typing import Optional

from hubspot import HubSpot
from hubspot.crm.contacts import (
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.exceptions import ApiException

logger = logging.getLogger(__name__)

# Standard HubSpot note-to-contact association type (HUBSPOT_DEFINED)
_NOTE_TO_CONTACT_TYPE_ID = 202


class HubSpotClient:
    def __init__(self, access_token: str):
        self._api = HubSpot(access_token=access_token)

    # ── contact operations ────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[object]:
        """Search for a contact by email address. Returns the first match or None."""
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        try:
            resp = self._api.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            return resp.results[0] if resp.total > 0 else None
        except ApiException as exc:
            logger.error("HubSpot search failed for %s: %s", email, exc)
            return None

    def create_contact(self, properties: dict) -> Optional[object]:
        """Create a new contact in HubSpot. Returns the created object or None."""
        try:
            contact = self._api.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=properties
                )
            )
            logger.info("Created HubSpot contact %s (id=%s)", properties.get("email"), contact.id)
            return contact
        except ApiException as exc:
            logger.error("HubSpot create failed for %s: %s", properties.get("email"), exc)
            return None

    def update_contact(self, contact_id: str, properties: dict) -> Optional[object]:
        """Update an existing contact's properties. Returns the updated object or None."""
        try:
            contact = self._api.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=properties
                ),
            )
            logger.info("Updated HubSpot contact id=%s fields=%s", contact_id, list(properties))
            return contact
        except ApiException as exc:
            logger.error("HubSpot update failed for id=%s: %s", contact_id, exc)
            return None

    # ── timeline notes ────────────────────────────────────────────────────────

    def create_note(self, contact_id: str, body: str) -> bool:
        """
        Create a timeline note and associate it with *contact_id*.
        Failures are logged but do not raise exceptions.
        """
        try:
            from hubspot.crm.objects import SimplePublicObjectInputForCreate as ObjCreate

            note = self._api.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=ObjCreate(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": str(int(time.time() * 1000)),
                    }
                )
            )
            self._associate_note(note.id, contact_id)
            return True
        except Exception as exc:
            logger.warning("Could not create timeline note for contact %s: %s", contact_id, exc)
            return False

    def _associate_note(self, note_id: str, contact_id: str) -> None:
        try:
            self._api.crm.associations.v4.basic_api.create(
                from_object_type="notes",
                from_object_id=note_id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_spec=[
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
                    }
                ],
            )
        except Exception as exc:
            logger.warning("Could not associate note %s to contact %s: %s", note_id, contact_id, exc)
