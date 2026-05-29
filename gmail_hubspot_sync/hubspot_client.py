import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup

from .contact_extractor import SenderContact
from .config import Config

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self, api_key: str):
        self._client = hubspot.Client.create(access_token=api_key)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the first HubSpot contact matching the email, or None."""
        email_filter = Filter(
            property_name="email",
            operator="EQ",
            value=email,
        )
        filter_group = FilterGroup(filters=[email_filter])
        search_request = PublicObjectSearchRequest(
            filter_groups=[filter_group],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.results:
                return response.results[0].to_dict()
        except ApiException as e:
            logger.error("HubSpot search error for %s: %s", email, e)
        return None

    def create_contact(self, sender: SenderContact) -> Optional[dict]:
        """Create a new HubSpot contact from sender data. Returns the created object."""
        props = self._build_properties(sender)
        obj = SimplePublicObjectInputForCreate(properties=props)
        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=obj
            )
            logger.info("Created contact %s (id=%s)", sender.email, result.id)
            return result.to_dict()
        except ApiException as e:
            logger.error("HubSpot create error for %s: %s", sender.email, e)
        return None

    def update_contact(self, contact_id: str, sender: SenderContact, existing: dict) -> Optional[dict]:
        """
        Update only fields that are currently empty in the existing contact.
        Returns the updated object or None on error.
        """
        existing_props = existing.get("properties", {})
        updates = {}

        if sender.first_name and not existing_props.get("firstname"):
            updates["firstname"] = sender.first_name
        if sender.last_name and not existing_props.get("lastname"):
            updates["lastname"] = sender.last_name
        if sender.company and not existing_props.get("company"):
            updates["company"] = sender.company

        if not updates:
            return existing  # nothing to patch

        from hubspot.crm.contacts import SimplePublicObjectInput
        obj = SimplePublicObjectInput(properties=updates)
        try:
            result = self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=obj,
            )
            logger.info("Updated contact %s (id=%s)", sender.email, contact_id)
            return result.to_dict()
        except ApiException as e:
            logger.error("HubSpot update error for %s: %s", sender.email, e)
        return None

    def _build_properties(self, sender: SenderContact) -> dict:
        props = {
            "email": sender.email,
            "hs_lead_status": "NEW",
            "leadsource": Config.CONTACT_SOURCE,
        }
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company
        return props
