"""HubSpot CRM client — contacts + optional timeline notes."""

import time
import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

logger = logging.getLogger(__name__)

_CONTACT_PROPS = ["email", "firstname", "lastname", "company", "hs_lead_source"]

# HubSpot built-in association type: Note → Contact
_NOTE_TO_CONTACT_ASSOC_TYPE_ID = 202


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return {id, properties} for the first contact matching *email*, or None."""
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(filters=[
                    Filter(property_name="email", operator="EQ", value=email)
                ])
            ],
            properties=_CONTACT_PROPS,
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            if resp.total > 0:
                r = resp.results[0]
                return {"id": r.id, "properties": r.properties}
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Create / Update
    # ------------------------------------------------------------------

    def create_contact(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
    ) -> Optional[str]:
        """Create a contact and return its ID, or None on failure."""
        props: dict = {"email": email, "hs_lead_source": "Gmail"}
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as exc:
            logger.error("HubSpot create contact failed for %s: %s", email, exc)
        return None

    def update_contact(self, contact_id: str, properties: dict) -> bool:
        """Patch an existing contact. Returns True on success."""
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=properties
                ),
            )
            return True
        except ApiException as exc:
            logger.error("HubSpot update %s failed: %s", contact_id, exc)
        return False

    # ------------------------------------------------------------------
    # Timeline / Notes  (optional)
    # ------------------------------------------------------------------

    def create_note(self, contact_id: str, body: str) -> bool:
        """
        Create a note associated with *contact_id*.
        Best-effort: logs a warning on failure but never raises.
        """
        try:
            from hubspot.crm.objects.notes.models import (
                SimplePublicObjectInputForCreate as NoteCreate,
            )

            note_input = NoteCreate(
                properties={
                    "hs_note_body": body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                },
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": _NOTE_TO_CONTACT_ASSOC_TYPE_ID,
                            }
                        ],
                    }
                ],
            )
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=note_input
            )
            return True
        except Exception as exc:
            logger.warning(
                "Could not create note for contact %s (non-critical): %s",
                contact_id,
                exc,
            )
        return False
