import logging
import time
from typing import Any, Optional

from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self, token: str):
        self.client = HubSpot(access_token=token)

    def find_contact_by_email(self, email: str) -> Optional[Any]:
        try:
            results = self.client.crm.contacts.search_api.do_search(
                public_object_search_request={
                    "filterGroups": [
                        {
                            "filters": [
                                {
                                    "propertyName": "email",
                                    "operator": "EQ",
                                    "value": email,
                                }
                            ]
                        }
                    ],
                    "properties": [
                        "email",
                        "firstname",
                        "lastname",
                        "company",
                        "lifecyclestage",
                    ],
                    "limit": 1,
                }
            )
            return results.results[0] if results.total > 0 else None
        except ApiException as e:
            logger.error(f"HubSpot search error for {email}: {e}")
            return None

    def create_contact(self, props: dict[str, str]) -> Optional[Any]:
        try:
            contact = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            logger.info(f"Created HubSpot contact: {props.get('email')} (ID: {contact.id})")
            return contact
        except ApiException as e:
            logger.error(f"HubSpot create error for {props.get('email')}: {e}")
            return None

    def update_contact(self, contact_id: str, props: dict[str, str]) -> Optional[Any]:
        try:
            contact = self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            logger.info(f"Updated HubSpot contact ID {contact_id}")
            return contact
        except ApiException as e:
            logger.error(f"HubSpot update error for ID {contact_id}: {e}")
            return None

    def add_activity_note(self, contact_id: str, body: str) -> Optional[Any]:
        """Creates a note engagement associated with a contact."""
        try:
            note = self.client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create={
                    "properties": {
                        "hs_note_body": body,
                        "hs_timestamp": str(int(time.time() * 1000)),
                    },
                    "associations": [
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,
                                }
                            ],
                        }
                    ],
                }
            )
            return note
        except Exception as e:
            logger.warning(f"Could not create activity note for contact {contact_id}: {e}")
            return None
