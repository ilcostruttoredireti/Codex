import logging
from typing import Optional, Dict, Any

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


class HubSpotClient:
    def __init__(self, api_key: str):
        self.client = hubspot.Client.create(access_token=api_key)

    def find_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """Return existing contact dict or None."""
        search = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[Filter(property_name="email", operator="EQ", value=email)]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            limit=1,
        )
        try:
            resp = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search
            )
            if resp.results:
                r = resp.results[0]
                return {"id": r.id, "properties": r.properties}
        except ApiException as e:
            logger.error("HubSpot search error for %s: %s", email, e)
        return None

    def create(self, props: Dict[str, str]) -> Optional[str]:
        """Create a new contact, return its ID or None on failure."""
        try:
            result = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as e:
            logger.error("HubSpot create error: %s", e)
        return None

    def update(self, contact_id: str, props: Dict[str, str]) -> bool:
        """Update fields on an existing contact."""
        try:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            return True
        except ApiException as e:
            logger.error("HubSpot update error for %s: %s", contact_id, e)
        return False
