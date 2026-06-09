"""HubSpot CRM client for contact creation and update."""

import logging
from typing import Dict, Optional, Tuple

from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInput, PublicObjectSearchRequest
from hubspot.crm.contacts.models import Filter, FilterGroup
from hubspot.crm.contacts.exceptions import ApiException

from .contact_extractor import ContactInfo

logger = logging.getLogger(__name__)

# HubSpot property used to track contact source
SOURCE_PROPERTY = "hs_lead_source"
SOURCE_VALUE = "Gmail"
TAG_PROPERTY = "hs_analytics_source"


class HubSpotClient:
    def __init__(self, access_token: str):
        self.client = HubSpot(access_token=access_token)

    def find_contact_by_email(self, email_addr: str) -> Optional[object]:
        """Return the first matching HubSpot contact or None."""
        search_filter = Filter(
            property_name="email",
            operator="EQ",
            value=email_addr,
        )
        search_request = PublicObjectSearchRequest(
            filter_groups=[FilterGroup(filters=[search_filter])],
            properties=["email", "firstname", "lastname", "company", SOURCE_PROPERTY],
        )
        try:
            result = self.client.crm.contacts.search_api.do_search(search_request)
            return result.results[0] if result.results else None
        except ApiException as exc:
            logger.error("HubSpot search failed for %s: %s", email_addr, exc)
            raise

    def create_contact(self, info: ContactInfo) -> str:
        """Create a new contact. Returns the HubSpot contact ID."""
        props = self._build_properties(info)
        try:
            resp = self.client.crm.contacts.basic_api.create(
                SimplePublicObjectInput(properties=props)
            )
            logger.info("Created contact %s (id=%s)", info.email, resp.id)
            return resp.id
        except ApiException as exc:
            logger.error("HubSpot create failed for %s: %s", info.email, exc)
            raise

    def update_contact(self, contact_id: str, info: ContactInfo, existing) -> bool:
        """
        Fill in only missing fields on an existing contact.
        Returns True if any field was actually updated.
        """
        existing_props = existing.properties or {}
        updates: Dict[str, str] = {}

        if info.first_name and not existing_props.get("firstname"):
            updates["firstname"] = info.first_name
        if info.last_name and not existing_props.get("lastname"):
            updates["lastname"] = info.last_name
        if info.company and not existing_props.get("company"):
            updates["company"] = info.company
        if not existing_props.get(SOURCE_PROPERTY):
            updates[SOURCE_PROPERTY] = SOURCE_VALUE

        if not updates:
            return False

        try:
            self.client.crm.contacts.basic_api.update(
                contact_id,
                SimplePublicObjectInput(properties=updates),
            )
            logger.info("Updated contact id=%s fields=%s", contact_id, list(updates))
            return True
        except ApiException as exc:
            logger.error("HubSpot update failed for id=%s: %s", contact_id, exc)
            raise

    def _build_properties(self, info: ContactInfo) -> Dict[str, str]:
        props: Dict[str, str] = {
            "email": info.email,
            SOURCE_PROPERTY: SOURCE_VALUE,
        }
        if info.first_name:
            props["firstname"] = info.first_name
        if info.last_name:
            props["lastname"] = info.last_name
        if info.company:
            props["company"] = info.company
        return props
