"""HubSpot API wrapper — contact lookup, create, update."""

import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
)
from hubspot.crm.contacts.models import Filter, FilterGroup

logger = logging.getLogger(__name__)

# HubSpot internal property names
_PROP_EMAIL = "email"
_PROP_FIRSTNAME = "firstname"
_PROP_LASTNAME = "lastname"
_PROP_COMPANY = "company"
_PROP_LEAD_SOURCE = "hs_lead_status"   # not source — use leadsource
_PROP_SOURCE = "leadsource"
_PROP_NOTES = "hs_content_membership_notes"


class HubSpotClient:
    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the HubSpot contact dict if *email* already exists, else None."""
        f = Filter(property_name=_PROP_EMAIL, operator="EQ", value=email.lower())
        fg = FilterGroup(filters=[f])
        req = PublicObjectSearchRequest(
            filter_groups=[fg],
            properties=[
                _PROP_EMAIL, _PROP_FIRSTNAME, _PROP_LASTNAME,
                _PROP_COMPANY, _PROP_SOURCE, "hs_tag",
            ],
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            if resp.results:
                c = resp.results[0]
                return {"id": c.id, "properties": c.properties}
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(
        self,
        email: str,
        firstname: str = "",
        lastname: str = "",
        company: str = "",
        source: str = "Gmail",
        notes: str = "",
    ) -> Optional[str]:
        """Create a new contact and return its HubSpot ID."""
        props = {_PROP_EMAIL: email.lower()}
        if firstname:
            props[_PROP_FIRSTNAME] = firstname
        if lastname:
            props[_PROP_LASTNAME] = lastname
        if company:
            props[_PROP_COMPANY] = company
        if source:
            props[_PROP_SOURCE] = source
        if notes:
            props[_PROP_NOTES] = notes

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as exc:
            # 409 = contact already exists (race condition)
            if exc.status == 409:
                logger.warning("Race-condition duplicate for %s, retrying lookup", email)
                existing = self.find_contact_by_email(email)
                return existing["id"] if existing else None
            logger.error("HubSpot create error for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, updates: dict) -> bool:
        """Patch *contact_id* with the non-empty fields in *updates*."""
        if not updates:
            return True
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=updates
                ),
            )
            return True
        except ApiException as exc:
            logger.error("HubSpot update error for %s: %s", contact_id, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline note (optional activity)
    # ------------------------------------------------------------------

    def log_email_activity(self, contact_id: str, subject: str, body: str) -> None:
        """Create a NOTE engagement linked to *contact_id*."""
        try:
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": _now_ms(),
                    },
                    associations=[
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
                )
            )
        except Exception as exc:
            logger.warning("Could not log activity for contact %s: %s", contact_id, exc)


def _now_ms() -> str:
    import time
    return str(int(time.time() * 1000))
