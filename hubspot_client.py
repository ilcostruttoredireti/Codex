"""HubSpot API client for contact management."""
import logging
from dataclasses import dataclass
from typing import Any

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.timeline import TimelineEvent, TimelineEventsApi

logger = logging.getLogger(__name__)

# HubSpot property names
_PROP_EMAIL = "email"
_PROP_FIRSTNAME = "firstname"
_PROP_LASTNAME = "lastname"
_PROP_COMPANY = "company"
_PROP_LEAD_SOURCE = "hs_lead_status"
_PROP_SOURCE = "leadsource"   # standard HubSpot property

# We store the inbound-Gmail tag in the contact's "hs_analytics_source" note
# via a custom property; fall back to a simple notes field.
_PROP_NOTES = "hs_content_membership_notes"

SOURCE_VALUE = "Gmail"
TAG_VALUE = "Inbound Gmail"


@dataclass
class ContactResult:
    status: str          # "created" | "updated" | "ignored"
    contact_email: str
    contact_id: str | None


class HubSpotClient:
    def __init__(self, api_key: str) -> None:
        self._client = hubspot.Client.create(access_token=api_key)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict[str, Any] | None:
        """Return the first matching HubSpot contact or None."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name=_PROP_EMAIL,
                            operator="EQ",
                            value=email,
                        )
                    ]
                )
            ],
            properties=[
                _PROP_EMAIL,
                _PROP_FIRSTNAME,
                _PROP_LASTNAME,
                _PROP_COMPANY,
                _PROP_SOURCE,
                _PROP_NOTES,
            ],
            limit=1,
        )
        try:
            result = self._client.crm.contacts.search_api.do_search(search_request)
            if result.results:
                return result.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Create / Update
    # ------------------------------------------------------------------

    def _build_properties(
        self,
        email: str,
        first_name: str,
        last_name: str,
        company: str,
        *,
        is_new: bool,
    ) -> dict[str, str]:
        props: dict[str, str] = {}

        if email:
            props[_PROP_EMAIL] = email
        if first_name:
            props[_PROP_FIRSTNAME] = first_name
        if last_name:
            props[_PROP_LASTNAME] = last_name
        if company:
            props[_PROP_COMPANY] = company

        if is_new:
            props[_PROP_SOURCE] = SOURCE_VALUE
            props[_PROP_NOTES] = TAG_VALUE

        return props

    def create_contact(
        self,
        email: str,
        first_name: str,
        last_name: str,
        company: str,
    ) -> str | None:
        """Create a new contact and return its ID, or None on failure."""
        props = self._build_properties(
            email, first_name, last_name, company, is_new=True
        )
        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            logger.info("Created HubSpot contact %s (%s)", email, contact.id)
            return contact.id
        except ApiException as exc:
            # 409 = already exists (race condition); treat as already present
            if exc.status == 409:
                logger.warning("Contact %s already exists (409), re-fetching", email)
                existing = self.find_contact_by_email(email)
                return existing.id if existing else None
            logger.error("Failed to create contact %s: %s", email, exc)
            return None

    def update_contact(
        self,
        contact_id: str,
        email: str,
        first_name: str,
        last_name: str,
        company: str,
        existing_props: dict[str, str],
    ) -> bool:
        """Fill in only the missing fields. Returns True on success."""
        updates: dict[str, str] = {}

        def _needs_update(prop: str, value: str) -> bool:
            return bool(value) and not existing_props.get(prop)

        if _needs_update(_PROP_FIRSTNAME, first_name):
            updates[_PROP_FIRSTNAME] = first_name
        if _needs_update(_PROP_LASTNAME, last_name):
            updates[_PROP_LASTNAME] = last_name
        if _needs_update(_PROP_COMPANY, company):
            updates[_PROP_COMPANY] = company
        if _needs_update(_PROP_SOURCE, SOURCE_VALUE):
            updates[_PROP_SOURCE] = SOURCE_VALUE
        if _needs_update(_PROP_NOTES, TAG_VALUE):
            updates[_PROP_NOTES] = TAG_VALUE

        if not updates:
            return False  # nothing to update

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input={"properties": updates},
            )
            logger.info(
                "Updated HubSpot contact %s (%s) fields: %s",
                email, contact_id, list(updates.keys()),
            )
            return True
        except ApiException as exc:
            logger.error("Failed to update contact %s: %s", contact_id, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline engagement
    # ------------------------------------------------------------------

    def log_email_received(
        self,
        contact_id: str,
        sender_email: str,
        subject: str,
    ) -> None:
        """Add a note engagement on the contact's timeline."""
        try:
            body = {
                "engagement": {"active": True, "type": "NOTE"},
                "associations": {
                    "contactIds": [int(contact_id)],
                    "companyIds": [],
                    "dealIds": [],
                    "ownerIds": [],
                    "ticketIds": [],
                },
                "metadata": {
                    "body": (
                        f"Inbound email received via Gmail\n"
                        f"From: {sender_email}\n"
                        f"Subject: {subject or '(no subject)'}\n"
                        f"Tag: {TAG_VALUE}"
                    )
                },
            }
            self._client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body=body,
                response_type=object,
                auth_settings=["hapikey"],
            )
        except Exception as exc:  # noqa: BLE001
            # Engagement logging is optional; don't crash the sync
            logger.warning("Could not log engagement for %s: %s", contact_id, exc)
