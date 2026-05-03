import os
import logging
from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException

logger = logging.getLogger(__name__)

CONTACT_PROPERTIES = ["email", "firstname", "lastname", "company", "hs_lead_status", "leadsource"]


class HubSpotClient:
    def __init__(self):
        token = os.getenv("HUBSPOT_ACCESS_TOKEN")
        if not token:
            raise EnvironmentError("HUBSPOT_ACCESS_TOKEN not set")
        self.client = HubSpot(access_token=token)

    def find_contact_by_email(self, email: str):
        """Return the first HubSpot contact matching the given email, or None."""
        try:
            search_request = PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(filters=[
                        Filter(property_name="email", operator="EQ", value=email)
                    ])
                ],
                properties=CONTACT_PROPERTIES,
                limit=1,
            )
            result = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            return result.results[0] if result.total > 0 else None
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
            return None

    def create_contact(self, data: dict):
        """Create a new HubSpot contact. Returns the created object or None on error."""
        try:
            props = self._base_properties(data)
            obj = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            self._add_inbound_gmail_tag(obj.id)
            return obj
        except ApiException as exc:
            logger.error("HubSpot create error for %s: %s", data.get("email"), exc)
            return None

    def update_contact(self, contact_id: str, data: dict):
        """Patch a HubSpot contact with non-empty values in data. Returns updated object or None."""
        try:
            props = {k: v for k, v in data.items() if v}
            if not props:
                return None
            obj = self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            return obj
        except ApiException as exc:
            logger.error("HubSpot update error for id %s: %s", contact_id, exc)
            return None

    def _add_inbound_gmail_tag(self, contact_id: str):
        """Append 'Inbound Gmail' to the hs_marketable_reason_name note via a timeline note.

        HubSpot free/starter tiers don't have custom timeline events, so we use
        the notes engagement API as a lightweight alternative.
        """
        try:
            self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": "Tag: Inbound Gmail",
                        "hs_timestamp": str(int(__import__("time").time() * 1000)),
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
                )
            )
        except Exception as exc:
            logger.debug("Could not add Gmail tag note for %s: %s", contact_id, exc)

    @staticmethod
    def _base_properties(data: dict) -> dict:
        props = {
            "email": data["email"],
            "leadsource": "Gmail",
        }
        if data.get("firstname"):
            props["firstname"] = data["firstname"]
        if data.get("lastname"):
            props["lastname"] = data["lastname"]
        if data.get("company"):
            props["company"] = data["company"]
        return props
