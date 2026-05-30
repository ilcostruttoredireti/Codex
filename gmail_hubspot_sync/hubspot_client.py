from typing import Optional

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicUpsertObject

from .config import HUBSPOT_ACCESS_TOKEN
from .contact_parser import SenderContact


_LEAD_SOURCE = "Gmail"
_TAG_PROPERTY = "hs_analytics_source_data_1"  # reuse source detail field for tag


class HubSpotClient:
    def __init__(self):
        self._client = hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        try:
            result = self._client.crm.contacts.basic_api.get_by_id(
                contact_id=email,
                id_property="email",
                properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            )
            return result.to_dict()
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def create_contact(self, contact: SenderContact) -> dict:
        properties = _build_properties(contact)
        body = SimplePublicObjectInputForCreate(properties=properties)
        result = self._client.crm.contacts.basic_api.create(simple_public_object_input_for_create=body)
        return result.to_dict()

    def update_contact(self, contact_id: str, contact: SenderContact, existing: dict) -> dict:
        existing_props = existing.get("properties", {})
        updates = {}

        if not existing_props.get("firstname") and contact.first_name:
            updates["firstname"] = contact.first_name
        if not existing_props.get("lastname") and contact.last_name:
            updates["lastname"] = contact.last_name
        if not existing_props.get("company") and contact.company:
            updates["company"] = contact.company

        # Always ensure source tag is present
        updates["hs_lead_source"] = _LEAD_SOURCE

        if not updates:
            return existing

        from hubspot.crm.contacts import SimplePublicObjectInput
        body = SimplePublicObjectInput(properties=updates)
        result = self._client.crm.contacts.basic_api.update(
            contact_id=contact_id, simple_public_object_input=body
        )
        return result.to_dict()

    def create_engagement_note(self, contact_id: str, contact: SenderContact) -> None:
        """Log an inbound email activity as a note on the contact timeline."""
        note_body = (
            f"Email inbound ricevuta via Gmail\n"
            f"Oggetto: {contact.subject}\n"
            f"Da: {contact.email}"
        )
        try:
            engagement = {
                "engagement": {"active": True, "type": "NOTE"},
                "associations": {"contactIds": [int(contact_id)]},
                "metadata": {"body": note_body},
            }
            self._client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body=engagement,
                response_type=object,
                auth_settings=["hapikey"],
            )
        except Exception:
            # Engagement creation is best-effort; don't fail the sync
            pass


def _build_properties(contact: SenderContact) -> dict:
    props = {
        "email": contact.email,
        "hs_lead_source": _LEAD_SOURCE,
    }
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    return props
