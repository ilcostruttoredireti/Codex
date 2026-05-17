"""HubSpot API client for contact management."""

import os
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

_ACCESS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")


def get_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=_ACCESS_TOKEN)


def find_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Return existing HubSpot contact dict or None."""
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
        resp = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_request
        )
        if resp.total > 0:
            result = resp.results[0]
            return {"id": result.id, "properties": result.properties}
    except ApiException:
        pass
    return None


def _build_properties(contact_info) -> dict:
    props = {
        "email": contact_info.email,
        "hs_lead_status": "NEW",
    }
    if contact_info.firstname:
        props["firstname"] = contact_info.firstname
    if contact_info.lastname:
        props["lastname"] = contact_info.lastname
    if contact_info.company:
        props["company"] = contact_info.company
    return props


def create_contact(client: hubspot.Client, contact_info) -> dict:
    """Create a new HubSpot contact and return {id, properties}."""
    props = _build_properties(contact_info)
    input_obj = SimplePublicObjectInput(properties=props)
    result = client.crm.contacts.basic_api.create(simple_public_object_input=input_obj)
    return {"id": result.id, "properties": result.properties}


def update_contact(client: hubspot.Client, contact_id: str, contact_info, existing: dict) -> dict:
    """Update a contact with any missing fields. Return {id, properties}."""
    existing_props = existing.get("properties", {})
    updates = {}

    if contact_info.firstname and not existing_props.get("firstname"):
        updates["firstname"] = contact_info.firstname
    if contact_info.lastname and not existing_props.get("lastname"):
        updates["lastname"] = contact_info.lastname
    if contact_info.company and not existing_props.get("company"):
        updates["company"] = contact_info.company

    if not updates:
        return existing

    input_obj = SimplePublicObjectInput(properties=updates)
    result = client.crm.contacts.basic_api.update(
        contact_id=contact_id, simple_public_object_input=input_obj
    )
    return {"id": result.id, "properties": result.properties}


def add_note(client: hubspot.Client, contact_id: str, subject: str, body_snippet: str) -> None:
    """Log an email-received engagement note on the contact."""
    try:
        note_body = f"Email ricevuta - Gmail\nOggetto: {subject}\n{body_snippet[:300]}"
        client.crm.objects.basic_api.create(
            object_type="notes",
            simple_public_object_input=SimplePublicObjectInput(
                properties={
                    "hs_note_body": note_body,
                    "hs_timestamp": str(int(__import__("time").time() * 1000)),
                }
            ),
        )
    except Exception:
        pass
