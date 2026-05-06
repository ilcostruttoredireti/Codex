import logging
from dataclasses import dataclass
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicUpsertObject

from config import HUBSPOT_ACCESS_TOKEN

logger = logging.getLogger("gmail_hubspot_sync.hubspot")

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"


@dataclass
class ContactData:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None


class HubSpotClient:
    def __init__(self) -> None:
        self._client = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the HubSpot contact dict if found, else None."""
        search_request = PublicObjectSearchRequest(
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
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0].to_dict()
        except ApiException as exc:
            logger.error("HubSpot search error: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, data: ContactData) -> Optional[str]:
        """Create a new contact and return its HubSpot ID."""
        properties = self._build_properties(data, is_new=True)
        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=properties
                )
            )
            contact_id = contact.id
            self._add_activity_note(contact_id, data.email)
            logger.info("Created HubSpot contact id=%s email=%s", contact_id, data.email)
            return contact_id
        except ApiException as exc:
            logger.error("HubSpot create error for %s: %s", data.email, exc)
            return None

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, existing: dict, data: ContactData) -> bool:
        """Fill in missing fields on an existing contact. Returns True if any update was made."""
        existing_props: dict = existing.get("properties", {})
        updates: dict[str, str] = {}

        def _missing(key: str) -> bool:
            val = existing_props.get(key)
            return not val or val.strip() == ""

        if _missing("firstname") and data.first_name:
            updates["firstname"] = data.first_name
        if _missing("lastname") and data.last_name:
            updates["lastname"] = data.last_name
        if _missing("company") and data.company:
            updates["company"] = data.company

        # Always add the tag (idempotent via semicolon-separated list)
        current_tags: str = existing_props.get("hs_analytics_source_data_1") or ""
        if CONTACT_TAG not in current_tags:
            existing_tags = [t.strip() for t in current_tags.split(";") if t.strip()]
            existing_tags.append(CONTACT_TAG)

        if not updates:
            logger.debug("No updates needed for contact id=%s.", contact_id)
            return False

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.SimplePublicObjectInput(
                    properties=updates
                ),
            )
            logger.info("Updated HubSpot contact id=%s fields=%s", contact_id, list(updates))
            return True
        except ApiException as exc:
            logger.error("HubSpot update error for id=%s: %s", contact_id, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline / activity note
    # ------------------------------------------------------------------

    def _add_activity_note(self, contact_id: str, sender_email: str) -> None:
        """Create an engagement note to record the inbound Gmail event."""
        try:
            self._client.crm.objects.basic_api.create(
                object_type="notes",
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": f"Inbound email received from {sender_email} via Gmail.",
                        "hs_timestamp": str(int(__import__("time").time() * 1000)),
                    },
                    associations=[
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,  # note → contact
                                }
                            ],
                        }
                    ],
                ),
            )
        except Exception as exc:  # notes API is non-critical
            logger.warning("Could not create activity note for contact %s: %s", contact_id, exc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_properties(self, data: ContactData, is_new: bool) -> dict[str, str]:
        props: dict[str, str] = {"email": data.email.lower()}

        if data.first_name:
            props["firstname"] = data.first_name
        if data.last_name:
            props["lastname"] = data.last_name
        if data.company:
            props["company"] = data.company

        if is_new:
            props["hs_lead_source"] = CONTACT_SOURCE

        return props
