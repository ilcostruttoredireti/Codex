"""
HubSpot CRM client - search, create, and update contacts.
Uses the official hubspot-api-client library with a Private App token.
"""
from __future__ import annotations

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInput, ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

CONTACT_PROPERTIES = [
    "email", "firstname", "lastname", "company",
    "lead_source", "hs_lead_status",
]


class HubSpotClient:
    def __init__(self, config) -> None:
        self._client = hubspot.Client.create(access_token=config.hubspot_token)
        self._source_label = config.contact_source_label

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_by_email(self, email: str) -> dict | None:
        """Return {'id': str, 'properties': dict} if contact exists, else None."""
        try:
            req = PublicObjectSearchRequest(
                filter_groups=[FilterGroup(filters=[
                    Filter(property_name="email", operator="EQ", value=email)
                ])],
                properties=CONTACT_PROPERTIES,
                limit=1,
            )
            result = self._client.crm.contacts.search_api.do_search(req)
            if result.results:
                r = result.results[0]
                return {"id": r.id, "properties": r.properties}
        except ApiException as exc:
            print(f"  [warn] HubSpot search failed for {email}: {exc}")
        return None

    def create(self, contact: dict) -> dict | None:
        """Create a new HubSpot contact. Returns {'id': str} or None on error."""
        try:
            props = self._build_create_props(contact)
            obj = SimplePublicObjectInput(properties=props)
            result = self._client.crm.contacts.basic_api.create(obj)
            return {"id": result.id}
        except ApiException as exc:
            print(f"  [warn] HubSpot create failed for {contact['email']}: {exc}")
        return None

    def update_missing_fields(self, contact_id: str, contact: dict, existing_props: dict) -> bool:
        """
        Fill in any empty fields on the existing contact.
        Returns True if an update was made.
        """
        updates: dict[str, str] = {}

        if not existing_props.get("company") and contact.get("company"):
            updates["company"] = contact["company"]
        if not existing_props.get("firstname") and contact.get("first_name"):
            updates["firstname"] = contact["first_name"]
        if not existing_props.get("lastname") and contact.get("last_name"):
            updates["lastname"] = contact["last_name"]
        if not existing_props.get("lead_source"):
            updates["lead_source"] = self._source_label
        if not existing_props.get("hs_lead_status"):
            updates["hs_lead_status"] = "NEW"

        if not updates:
            return False

        try:
            obj = SimplePublicObjectInput(properties=updates)
            self._client.crm.contacts.basic_api.update(contact_id, obj)
            return True
        except ApiException as exc:
            print(f"  [warn] HubSpot update failed for {contact_id}: {exc}")
        return False

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_create_props(self, contact: dict) -> dict[str, str]:
        return {
            "email": contact["email"],
            "firstname": contact.get("first_name", ""),
            "lastname": contact.get("last_name", ""),
            "company": contact.get("company", ""),
            "lead_source": self._source_label,
            "hs_lead_status": "NEW",
        }
