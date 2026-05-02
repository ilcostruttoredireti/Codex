from __future__ import annotations

import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
)

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the first matching contact dict, or None."""
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
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0].to_dict()
        except ApiException as exc:
            logger.error("HubSpot search error: %s", exc)
        return None

    def create_contact(self, properties: dict[str, str]) -> dict:
        """Create a new contact and return its dict representation."""
        obj_input = SimplePublicObjectInputForCreate(properties=properties)
        response = self._client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj_input
        )
        return response.to_dict()

    def update_contact(self, contact_id: str, properties: dict[str, str]) -> dict:
        """Patch an existing contact with the given properties."""
        obj_input = SimplePublicObjectInput(properties=properties)
        response = self._client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj_input,
        )
        return response.to_dict()

    # ------------------------------------------------------------------
    # Timeline / notes (optional)
    # ------------------------------------------------------------------

    def add_email_note(
        self,
        contact_id: str,
        sender_email: str,
        subject: str,
        timestamp_ms: str,
    ) -> None:
        """Attach a note to a contact recording the inbound Gmail email."""
        note_body = (
            f"Inbound Gmail received\n"
            f"From: {sender_email}\n"
            f"Subject: {subject}\n"
            f"Tag: Inbound Gmail"
        )
        note_input = {
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": timestamp_ms,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            # 202 = Note → Contact (HubSpot built-in type)
                            "associationTypeId": 202,
                        }
                    ],
                }
            ],
        }
        try:
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=note_input
            )
        except Exception as exc:
            # Timeline notes are optional; log but do not fail the sync
            logger.warning("Could not create timeline note: %s", exc)
