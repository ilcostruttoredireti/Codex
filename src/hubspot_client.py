import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup

from .contact_extractor import Contact

logger = logging.getLogger(__name__)

# HubSpot association type: Note → Contact
NOTE_TO_CONTACT_TYPE_ID = 202


class HubSpotClient:
    def __init__(self, access_token: str):
        self.client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[Filter(property_name="email", operator="EQ", value=email)]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "leadsource"],
            limit=1,
        )
        try:
            response = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0]
        except ApiException as e:
            logger.error("Error searching contact %s: %s", email, e)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, contact: Contact) -> Optional[str]:
        props = {
            "email": contact.email,
            "leadsource": "Gmail",
        }
        if contact.first_name:
            props["firstname"] = contact.first_name
        if contact.last_name:
            props["lastname"] = contact.last_name
        if contact.company:
            props["company"] = contact.company

        try:
            result = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as e:
            logger.error("Error creating contact %s: %s", contact.email, e)
            return None

    # ------------------------------------------------------------------
    # Update (only fill missing fields)
    # ------------------------------------------------------------------

    def update_contact_if_needed(self, contact_id: str, contact: Contact, existing) -> bool:
        existing_props = existing.properties or {}
        updates = {}

        if contact.first_name and not existing_props.get("firstname"):
            updates["firstname"] = contact.first_name
        if contact.last_name and not existing_props.get("lastname"):
            updates["lastname"] = contact.last_name
        if contact.company and not existing_props.get("company"):
            updates["company"] = contact.company
        if not existing_props.get("leadsource"):
            updates["leadsource"] = "Gmail"

        if not updates:
            return True

        try:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            logger.debug("Updated contact %s with %s", contact_id, list(updates.keys()))
            return True
        except ApiException as e:
            logger.error("Error updating contact %s: %s", contact_id, e)
            return False

    # ------------------------------------------------------------------
    # Timeline activity (note)
    # ------------------------------------------------------------------

    def add_email_activity(self, contact_id: str, subject: str, sender_email: str) -> bool:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        body = (
            f"📧 Email in entrata ricevuta\n"
            f"Da: {sender_email}\n"
            f"Oggetto: {subject}\n"
            f"Tag: Inbound Gmail"
        )

        try:
            from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput

            note = self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteInput(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": str(now_ms),
                    },
                    associations=[
                        {
                            "to": {"id": int(contact_id)},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": NOTE_TO_CONTACT_TYPE_ID,
                                }
                            ],
                        }
                    ],
                )
            )
            logger.debug("Added activity note %s to contact %s", note.id, contact_id)
            return True
        except Exception as e:
            logger.error("Error adding activity to contact %s: %s", contact_id, e)
            return False
