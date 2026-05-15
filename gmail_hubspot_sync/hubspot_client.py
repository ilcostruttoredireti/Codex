"""
HubSpot contact sync client.

Required env var:
  HUBSPOT_TOKEN   Private app token (Settings → Integrations → Private Apps)

Scopes needed on the private app:
  crm.objects.contacts.read
  crm.objects.contacts.write
  crm.objects.timeline.write  (optional, for activity events)
"""

import os
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi

SOURCE_LABEL = "Gmail"
INBOUND_TAG = "Inbound Gmail"


class HubSpotClient:
    def __init__(self, token: str):
        self._client = hubspot.Client.create(access_token=token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_contact(self, contact: dict) -> dict:
        """
        Upsert a contact in HubSpot.

        Returns:
          {"status": "created"|"updated"|"ignored", "email": str, "id": str}
        """
        email = contact.get("email", "").lower().strip()
        if not email:
            return {"status": "ignored", "email": "", "id": ""}

        existing = self._find_by_email(email)

        if existing:
            updated = self._update_contact(existing["id"], contact, existing["properties"])
            return {
                "status": "updated" if updated else "ignored",
                "email": email,
                "id": existing["id"],
            }
        else:
            new_id = self._create_contact(contact)
            return {"status": "created", "email": email, "id": new_id}

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _find_by_email(self, email: str) -> Optional[dict]:
        from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

        f = Filter(property_name="email", operator="EQ", value=email)
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            if resp.total > 0:
                result = resp.results[0]
                return {"id": result.id, "properties": result.properties}
        except ApiException:
            pass
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def _create_contact(self, contact: dict) -> str:
        props = self._build_properties(contact, existing_props={})
        obj = SimplePublicObjectInputForCreate(properties=props)
        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=obj
            )
            return result.id
        except ApiException as exc:
            # 409 = already exists (race condition) → try to find it
            if "409" in str(exc) or "CONTACT_EXISTS" in str(exc):
                existing = self._find_by_email(contact["email"])
                if existing:
                    return existing["id"]
            raise

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def _update_contact(
        self, contact_id: str, contact: dict, existing_props: dict
    ) -> bool:
        """Fill in only the missing fields. Returns True if any field was written."""
        updates = {}

        def _fill(hs_key: str, new_val: Optional[str]) -> None:
            if new_val and not existing_props.get(hs_key):
                updates[hs_key] = new_val

        _fill("firstname", contact.get("first_name"))
        _fill("lastname", contact.get("last_name"))
        _fill("company", contact.get("company"))

        # Always ensure source label and tag are set
        if existing_props.get("lead_source") != SOURCE_LABEL:
            updates["lead_source"] = SOURCE_LABEL

        if not updates:
            return False

        obj = SimplePublicObjectInput(properties=updates)
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=obj,
            )
            return True
        except ApiException:
            return False

    # ------------------------------------------------------------------
    # Property builder
    # ------------------------------------------------------------------

    def _build_properties(self, contact: dict, existing_props: dict) -> dict:
        props: dict = {"email": contact["email"]}

        def _set(hs_key: str, val: Optional[str]) -> None:
            if val and not existing_props.get(hs_key):
                props[hs_key] = val

        _set("firstname", contact.get("first_name"))
        _set("lastname", contact.get("last_name"))
        _set("company", contact.get("company"))
        props["lead_source"] = SOURCE_LABEL

        return props
