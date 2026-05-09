from datetime import datetime, timezone

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, SimplePublicObjectInput


class HubSpotClient:
    """Thin wrapper around the HubSpot Python SDK for contact management."""

    CONTACT_PROPS = ["firstname", "lastname", "email", "company", "hs_lead_source"]

    def __init__(self, access_token: str):
        self.client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Contact lookup / create / update
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str):
        """Return the first matching contact object or None."""
        try:
            filter_ = Filter(property_name="email", operator="EQ", value=email)
            fg = FilterGroup(filters=[filter_])
            req = PublicObjectSearchRequest(
                filter_groups=[fg],
                properties=self.CONTACT_PROPS,
                limit=1,
            )
            resp = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            return resp.results[0] if resp.results else None
        except ApiException as exc:
            raise RuntimeError(f"HubSpot search failed: {exc}") from exc

    def create_contact(self, properties: dict):
        """Create and return a new HubSpot contact."""
        try:
            obj = SimplePublicObjectInputForCreate(properties=properties)
            return self.client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=obj
            )
        except ApiException as exc:
            raise RuntimeError(f"HubSpot create failed: {exc}") from exc

    def update_contact(self, contact_id: str, properties: dict):
        """Patch an existing contact with the supplied properties."""
        try:
            obj = SimplePublicObjectInput(properties=properties)
            return self.client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=obj,
            )
        except ApiException as exc:
            raise RuntimeError(f"HubSpot update failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Activity timeline (email engagement)
    # ------------------------------------------------------------------

    def log_inbound_email(
        self,
        contact_id: str,
        subject: str,
        received_at: str,
    ) -> None:
        """
        Create an 'Inbound email' engagement on the contact timeline.
        Non-fatal: errors are printed but do not abort the sync.
        """
        try:
            from hubspot.crm.objects.emails import (
                SimplePublicObjectInputForCreate as EmailInput,
            )

            properties = {
                "hs_email_direction": "INCOMING_EMAIL",
                "hs_email_subject": subject or "(no subject)",
                "hs_email_status": "RECEIVED",
                "hs_timestamp": received_at,
            }
            # Association type 198 = email → contact (HubSpot standard)
            email_obj = EmailInput(
                properties=properties,
                associations=[
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 198,
                            }
                        ],
                    }
                ],
            )
            self.client.crm.objects.emails.basic_api.create(
                simple_public_object_input_for_create=email_obj
            )
        except Exception as exc:
            print(f"    ⚠  Timeline engagement skipped: {exc}")
