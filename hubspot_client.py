import logging
import time

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

from config import HUBSPOT_ACCESS_TOKEN, CONTACT_SOURCE, INBOUND_TAG

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self):
        self.client = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)

    # ------------------------------------------------------------------ #
    # Contact search / create / update                                     #
    # ------------------------------------------------------------------ #

    def find_contact_by_email(self, email: str):
        """Return existing contact object or None."""
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[Filter(property_name="email", operator="EQ", value=email)]
                )
            ],
            properties=[
                "email", "firstname", "lastname", "company", "leadsource",
            ],
            limit=1,
        )
        try:
            result = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            if result.total > 0:
                return result.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    def create_contact(self, data: dict):
        """Create a new HubSpot contact. Returns the created object or None."""
        props = {
            "email": data["email"],
            "firstname": data.get("first_name", ""),
            "lastname": data.get("last_name", ""),
            "leadsource": CONTACT_SOURCE,
        }
        if data.get("company"):
            props["company"] = data["company"]

        try:
            contact = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            logger.info("Created contact %s → ID %s", data["email"], contact.id)
            return contact
        except ApiException as exc:
            logger.error("HubSpot create error for %s: %s", data["email"], exc)
            return None

    def update_contact(self, contact_id: str, updates: dict):
        """Patch an existing contact with the provided fields."""
        try:
            contact = self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=updates
                ),
            )
            logger.info("Updated contact ID %s with %s", contact_id, list(updates.keys()))
            return contact
        except ApiException as exc:
            logger.error("HubSpot update error for ID %s: %s", contact_id, exc)
            return None

    def build_updates(self, existing, data: dict) -> dict:
        """Return a dict of fields that are empty on the contact but present in data."""
        props = existing.properties or {}
        updates: dict = {}

        if not props.get("firstname") and data.get("first_name"):
            updates["firstname"] = data["first_name"]
        if not props.get("lastname") and data.get("last_name"):
            updates["lastname"] = data["last_name"]
        if not props.get("company") and data.get("company"):
            updates["company"] = data["company"]
        if not props.get("leadsource"):
            updates["leadsource"] = CONTACT_SOURCE

        return updates

    # ------------------------------------------------------------------ #
    # Timeline note                                                        #
    # ------------------------------------------------------------------ #

    def add_email_note(self, contact_id: str, data: dict) -> None:
        """Add a CRM note to the contact timeline recording the inbound email."""
        body = (
            f"Email inbound ricevuta da {data['display_name']} <{data['email']}>.\n"
            f"Oggetto: {data.get('subject', '—')}\n"
            f"Data: {data.get('date', '—')}\n"
            f"Tag: {INBOUND_TAG}"
        )
        timestamp_ms = str(int(time.time() * 1000))
        note_props = {
            "hs_note_body": body,
            "hs_timestamp": timestamp_ms,
        }
        associations = [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,
                    }
                ],
            }
        ]
        try:
            self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create={
                    "properties": note_props,
                    "associations": associations,
                }
            )
            logger.info("Note added to contact ID %s", contact_id)
        except Exception as exc:
            # Non-critical — log and continue
            logger.warning("Could not add note to contact %s: %s", contact_id, exc)
