"""
HubSpot CRM client — search, create, and update contacts.
"""

import os
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    PublicObjectSearchRequest,
)
from hubspot.crm.contacts.exceptions import ApiException

from contact_parser import ContactInfo

_SOURCE_LABEL = "Gmail"
_TAG_PROPERTY = "hs_lead_source"  # built-in HubSpot lead-source field


class HubSpotClient:
    def __init__(self, access_token: Optional[str] = None):
        token = access_token or os.environ["HUBSPOT_ACCESS_TOKEN"]
        self._client = hubspot.Client.create(access_token=token)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact dict or None."""
        req = PublicObjectSearchRequest(
            filter_groups=[
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            limit=1,
        )
        resp = self._client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.results:
            return resp.results[0]
        return None

    def create_contact(self, info: ContactInfo) -> dict:
        props = _build_properties(info)
        body = SimplePublicObjectInputForCreate(properties=props)
        return self._client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=body
        )

    def update_contact(self, contact_id: str, info: ContactInfo, existing: dict) -> dict:
        """Only patch fields that are currently empty in HubSpot."""
        current = existing.properties if hasattr(existing, "properties") else existing.get("properties", {})
        props = {}
        new_props = _build_properties(info)
        for key, val in new_props.items():
            if not current.get(key) and val:
                props[key] = val
        if not props:
            return existing  # nothing to update
        from hubspot.crm.contacts import SimplePublicObjectInput
        body = SimplePublicObjectInput(properties=props)
        return self._client.crm.contacts.basic_api.update(
            contact_id=contact_id, simple_public_object_input=body
        )

    def create_email_activity(self, contact_id: str, subject: str, message_id: str) -> None:
        """Log an inbound email engagement on the contact timeline."""
        try:
            self._client.crm.objects.emails.basic_api.create(
                simple_public_object_input_for_create={
                    "properties": {
                        "hs_email_direction": "INCOMING_EMAIL",
                        "hs_email_subject": subject or "(no subject)",
                        "hs_email_status": "RECEIVED",
                    },
                    "associations": [
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}
                            ],
                        }
                    ],
                }
            )
        except Exception:
            pass  # activity logging is best-effort


def _build_properties(info: ContactInfo) -> dict:
    props: dict = {"email": info.email, "hs_lead_source": _SOURCE_LABEL}
    if info.first_name:
        props["firstname"] = info.first_name
    if info.last_name:
        props["lastname"] = info.last_name
    if info.company:
        props["company"] = info.company
    return props
