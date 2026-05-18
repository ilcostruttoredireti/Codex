"""HubSpot API client: contact lookup, create, update and timeline events."""

import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    ApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

from .config import HUBSPOT_ACCESS_TOKEN

logger = logging.getLogger(__name__)

SOURCE_LABEL = "Gmail"
INBOUND_TAG = "Inbound Gmail"


class HubSpotClient:
    def __init__(self):
        self._client = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the HubSpot contact dict or None if not found."""
        filter_ = Filter(property_name="email", operator="EQ", value=email)
        filter_group = FilterGroup(filters=[filter_])
        request = PublicObjectSearchRequest(
            filter_groups=[filter_group],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status",
                        "lifecyclestage"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=request
            )
            results = response.results
            if results:
                return {"id": results[0].id, "properties": results[0].properties}
            return None
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
            return None

    def create_contact(self, email: str, first_name: Optional[str],
                       last_name: Optional[str], company: Optional[str]) -> Optional[str]:
        """Create a new contact. Returns the HubSpot contact ID or None on failure."""
        properties = self._build_properties(email, first_name, last_name, company)
        body = SimplePublicObjectInputForCreate(properties=properties)
        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=body
            )
            logger.info("Created contact %s (id=%s)", email, result.id)
            return result.id
        except ApiException as exc:
            logger.error("HubSpot create error for %s: %s", email, exc)
            return None

    def update_contact(self, contact_id: str, existing: dict,
                       first_name: Optional[str], last_name: Optional[str],
                       company: Optional[str]) -> bool:
        """Fill in only blank fields on an existing contact. Returns True if any update was sent."""
        props = existing.get("properties", {})
        updates: dict[str, str] = {}

        if first_name and not props.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not props.get("lastname"):
            updates["lastname"] = last_name
        if company and not props.get("company"):
            updates["company"] = company

        if not updates:
            return False

        from hubspot.crm.contacts import SimplePublicObjectInput
        body = SimplePublicObjectInput(properties=updates)
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=body,
            )
            logger.info("Updated contact id=%s with %s", contact_id, list(updates.keys()))
            return True
        except ApiException as exc:
            logger.error("HubSpot update error for id=%s: %s", contact_id, exc)
            return False

    def log_email_activity(self, contact_id: str, email: str,
                           subject: str, message_id: str) -> None:
        """Create a timeline/note event recording the inbound email."""
        note_body = (
            f"Inbound email received via Gmail\n"
            f"From: {email}\n"
            f"Subject: {subject or '(no subject)'}\n"
            f"Gmail message ID: {message_id}"
        )
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
        properties = {
            "hs_note_body": note_body,
            "hs_timestamp": _now_ms(),
        }
        associations = [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202}],  # note → contact
            }
        ]
        try:
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteInput(
                    properties=properties,
                    associations=associations,
                )
            )
        except ApiException as exc:
            # Non-fatal: log the warning but don't abort the sync
            logger.warning("Could not create note for contact %s: %s", contact_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_properties(email: str, first_name: Optional[str],
                           last_name: Optional[str], company: Optional[str]) -> dict:
        props: dict[str, str] = {
            "email": email,
            "hs_lead_status": "NEW",
            "lifecyclestage": "lead",
            "lead_source": SOURCE_LABEL,
        }
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company
        return props


def _now_ms() -> str:
    import time
    return str(int(time.time() * 1000))
