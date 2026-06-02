"""HubSpot CRM client for contact management."""

from typing import Optional
from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
    PublicObjectSearchRequest,
    Filter,
    FilterGroup,
)
from hubspot.crm.contacts.exceptions import ApiException


class HubSpotClient:
    def __init__(self, access_token: str):
        self.client = HubSpot(access_token=access_token)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Search for a contact by email. Returns contact dict or None."""
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
            result = self.client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if result.results:
                contact = result.results[0]
                return {"id": contact.id, "properties": contact.properties}
        except ApiException:
            pass
        return None

    def create_contact(self, props: dict) -> dict:
        """Create a new HubSpot contact. Returns the created contact dict."""
        contact_input = SimplePublicObjectInputForCreate(properties=props)
        result = self.client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=contact_input
        )
        return {"id": result.id, "properties": result.properties}

    def update_contact(self, contact_id: str, props: dict) -> dict:
        """Update an existing contact (only non-empty fields)."""
        contact_input = SimplePublicObjectInput(properties=props)
        result = self.client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=contact_input,
        )
        return {"id": result.id, "properties": result.properties}

    def add_email_activity(self, contact_id: str, subject: str, from_email: str, date: str):
        """Log an inbound email activity on the contact's timeline."""
        try:
            self.client.crm.objects.basic_api.create(
                object_type="engagements",
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties={
                        "hs_timestamp": date,
                        "hs_engagement_type": "EMAIL",
                        "hs_email_direction": "INCOMING_EMAIL",
                        "hs_email_subject": subject,
                        "hs_email_from_email": from_email,
                    },
                    associations=[
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 9,  # contact → engagement
                                }
                            ],
                        }
                    ],
                ),
            )
        except Exception:
            pass  # Timeline activity is optional; don't fail the sync


def build_contact_props(sender: dict, existing_props: Optional[dict] = None) -> dict:
    """
    Build the HubSpot property dict for create/update.
    For updates, only includes fields that are currently blank.
    """
    candidate = {
        "email": sender["email"],
        "firstname": sender["first_name"],
        "lastname": sender["last_name"],
        "company": sender["company"],
        "hs_lead_source": "Gmail",
    }

    if existing_props is None:
        # Creating a new contact — include all non-empty fields
        return {k: v for k, v in candidate.items() if v}

    # Updating — only fill in fields that are currently empty/missing
    updates = {}
    for key, value in candidate.items():
        if key == "email":
            continue  # never change the primary email key
        if value and not existing_props.get(key):
            updates[key] = value
    return updates
