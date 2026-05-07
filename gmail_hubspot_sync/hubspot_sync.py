"""HubSpot CRM sync – create, update and tag contacts."""

import logging
from dataclasses import dataclass
from enum import Enum

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    PublicObjectSearchRequest,
    ApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup
from hubspot.crm.timeline.models import TimelineEvent

logger = logging.getLogger(__name__)

SOURCE_LABEL = "Gmail"
INBOUND_TAG = "Inbound Gmail"


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str | None


class HubSpotSync:
    def __init__(self, api_key: str, app_id: str | None = None):
        self.client = hubspot.Client.create(access_token=api_key)
        self.app_id = app_id  # needed for timeline events

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def sync_contact(self, contact_info: dict, email_metadata: dict | None = None) -> SyncResult:
        """
        Upsert a contact in HubSpot.

        contact_info keys: email, first_name, last_name, company, domain, full_name
        email_metadata keys: subject, date, message_id (optional)
        """
        email = contact_info["email"]

        existing = self._find_by_email(email)

        if existing:
            contact_id = existing["id"]
            updated = self._update_missing_fields(contact_id, existing["properties"], contact_info)
            status = SyncStatus.UPDATED if updated else SyncStatus.SKIPPED
        else:
            contact_id = self._create_contact(contact_info)
            status = SyncStatus.CREATED

        if contact_id and email_metadata:
            self._log_email_activity(contact_id, email_metadata)

        return SyncResult(status=status, email=email, contact_id=contact_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_by_email(self, email: str) -> dict | None:
        """Search HubSpot contacts by email address."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email,
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            response = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.results:
                result = response.results[0]
                return {"id": result.id, "properties": result.properties}
        except ApiException as e:
            logger.error("HubSpot search error for %s: %s", email, e)
        return None

    def _create_contact(self, info: dict) -> str | None:
        """Create a new HubSpot contact and return its ID."""
        props = self._build_properties(info)
        props["hs_lead_status"] = SOURCE_LABEL

        body = SimplePublicObjectInputForCreate(properties=props)
        try:
            result = self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=body
            )
            logger.info("Created contact %s (id=%s)", info["email"], result.id)
            return result.id
        except ApiException as e:
            logger.error("Failed to create contact %s: %s", info["email"], e)
            return None

    def _update_missing_fields(
        self, contact_id: str, existing_props: dict, new_info: dict
    ) -> bool:
        """Fill in any blank fields from new_info. Returns True if any update was made."""
        updates = {}

        field_map = {
            "firstname": new_info.get("first_name"),
            "lastname": new_info.get("last_name"),
            "company": new_info.get("company"),
        }

        for hs_field, value in field_map.items():
            if value and not existing_props.get(hs_field):
                updates[hs_field] = value

        if not updates:
            return False

        try:
            self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input={"properties": updates},
            )
            logger.info("Updated contact %s: %s", contact_id, list(updates.keys()))
            return True
        except ApiException as e:
            logger.error("Failed to update contact %s: %s", contact_id, e)
            return False

    def _build_properties(self, info: dict) -> dict:
        props: dict = {"email": info["email"]}

        if info.get("first_name"):
            props["firstname"] = info["first_name"]
        if info.get("last_name"):
            props["lastname"] = info["last_name"]
        if info.get("company"):
            props["company"] = info["company"]

        # Source tracking
        props["leadsource"] = SOURCE_LABEL

        return props

    def _log_email_activity(self, contact_id: str, email_metadata: dict) -> None:
        """Create a HubSpot note engagement to record the received email."""
        subject = email_metadata.get("subject", "(no subject)")
        date = email_metadata.get("date", "")
        note_body = f"[{INBOUND_TAG}] Email ricevuta\nOggetto: {subject}\nData: {date}"

        try:
            engagement_payload = {
                "engagement": {
                    "active": True,
                    "type": "NOTE",
                },
                "associations": {
                    "contactIds": [int(contact_id)],
                },
                "metadata": {
                    "body": note_body,
                },
            }
            # Use legacy engagements API (v1) which doesn't require app_id
            self.client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body=engagement_payload,
                response_type=object,
                auth_settings=["hapikey"],
                _return_http_data_only=True,
            )
            logger.debug("Logged email activity for contact %s", contact_id)
        except Exception as e:
            # Non-critical – log and continue
            logger.warning("Could not log email activity for contact %s: %s", contact_id, e)
