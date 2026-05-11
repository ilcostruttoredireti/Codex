import logging
import time
from typing import Optional, Tuple

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

from config import (
    CONTACT_SOURCE,
    HUBSPOT_ACCESS_TOKEN,
    HUBSPOT_NOTE_TO_CONTACT_TYPE_ID,
    INBOUND_TAG,
)
from contact_extractor import ContactInfo

logger = logging.getLogger(__name__)

STATUS_CREATED = "Creato"
STATUS_UPDATED = "Aggiornato"
STATUS_IGNORED = "Ignorato"


class HubSpotSync:
    def __init__(self):
        self.client = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)

    def sync_contact(self, contact: ContactInfo) -> Tuple[str, Optional[str]]:
        existing = self._find_by_email(contact.email)

        if existing:
            contact_id = existing.id
            was_updated = self._fill_missing_fields(contact_id, existing.properties, contact)
            status = STATUS_UPDATED if was_updated else STATUS_IGNORED
        else:
            contact_id = self._create(contact)
            status = STATUS_CREATED if contact_id else STATUS_IGNORED

        if contact_id and status in (STATUS_CREATED, STATUS_UPDATED):
            self._add_note(contact_id, contact)

        return status, contact_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_by_email(self, email: str):
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[Filter(property_name="email", operator="EQ", value=email)]
                )
            ],
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        try:
            response = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            return response.results[0] if response.results else None
        except ApiException as exc:
            logger.error("HubSpot search failed for %s: %s", email, exc)
            return None

    def _create(self, contact: ContactInfo) -> Optional[str]:
        properties = {
            "email": contact.email,
            "lead_source": CONTACT_SOURCE,
        }
        if contact.first_name:
            properties["firstname"] = contact.first_name
        if contact.last_name:
            properties["lastname"] = contact.last_name
        if contact.company:
            properties["company"] = contact.company

        try:
            response = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=properties
                )
            )
            logger.info("Created HubSpot contact %s → ID %s", contact.email, response.id)
            return response.id
        except ApiException as exc:
            logger.error("HubSpot create failed for %s: %s", contact.email, exc)
            return None

    def _fill_missing_fields(
        self, contact_id: str, existing: dict, contact: ContactInfo
    ) -> bool:
        updates: dict = {}
        if not existing.get("firstname") and contact.first_name:
            updates["firstname"] = contact.first_name
        if not existing.get("lastname") and contact.last_name:
            updates["lastname"] = contact.last_name
        if not existing.get("company") and contact.company:
            updates["company"] = contact.company

        if not updates:
            return False

        try:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            logger.info("Updated contact %s: %s", contact_id, list(updates))
            return True
        except ApiException as exc:
            logger.error("HubSpot update failed for %s: %s", contact_id, exc)
            return False

    def _add_note(self, contact_id: str, contact: ContactInfo) -> None:
        body = (
            f"Email in entrata ricevuta\n"
            f"Da: {contact.email}\n"
            f"Oggetto: {contact.subject or 'N/A'}\n"
            f"Data: {contact.received_at or 'N/A'}\n"
            f"Tag: {INBOUND_TAG}"
        )
        try:
            from hubspot.crm.objects.notes import (
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
                                "associationTypeId": HUBSPOT_NOTE_TO_CONTACT_TYPE_ID,
                            }
                        ],
                    }
                ],
            )
            self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=note_input
            )
        except Exception as exc:
            logger.warning("Could not add timeline note for %s: %s", contact_id, exc)
