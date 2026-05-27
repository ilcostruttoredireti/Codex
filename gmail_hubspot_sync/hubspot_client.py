"""HubSpot CRM client — create / update contacts, add activity notes."""

import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

from config import CONTACT_SOURCE, CONTACT_TAG, HUBSPOT_ACCESS_TOKEN

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self):
        self._client = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)
        self._contacts: BasicApi = self._client.crm.contacts.basic_api
        self._search: SearchApi = self._client.crm.contacts.search_api

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_by_email(self, email: str) -> Optional[dict]:
        """Return the first matching HubSpot contact or None."""
        request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email.lower(),
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            resp = self._search.do_search(public_object_search_request=request)
            if resp.total > 0:
                return resp.results[0].to_dict()
        except ApiException as exc:
            logger.error("HubSpot search failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, props: dict) -> Optional[dict]:
        payload = SimplePublicObjectInputForCreate(properties=props)
        try:
            result = self._contacts.create(
                simple_public_object_input_for_create=payload
            )
            return result.to_dict()
        except ApiException as exc:
            logger.error("HubSpot create failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, props: dict) -> Optional[dict]:
        payload = SimplePublicObjectInput(properties=props)
        try:
            result = self._contacts.update(
                contact_id=contact_id,
                simple_public_object_input=payload,
            )
            return result.to_dict()
        except ApiException as exc:
            logger.error("HubSpot update failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def build_props(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
    ) -> dict:
        props: dict = {"email": email, "hs_lead_status": "NEW"}
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company
        props["leadsource"] = CONTACT_SOURCE
        # Custom tag stored in a note property; actual tag support depends on portal plan
        props["hs_content_membership_notes"] = CONTACT_TAG
        return props

    def merge_missing_fields(self, existing: dict, new_props: dict) -> dict:
        """Return only the fields from new_props that are absent/blank in existing."""
        current = existing.get("properties", {})
        updates: dict = {}
        for key, value in new_props.items():
            if key in ("email",):  # never overwrite email
                continue
            existing_value = current.get(key)
            if not existing_value and value:
                updates[key] = value
        return updates
