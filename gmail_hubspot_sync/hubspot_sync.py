"""
HubSpot API integration: find, create, and update contacts.
Uses the official HubSpot Python SDK.
"""
import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import PublicObjectSearchRequest

from .config import HUBSPOT_ACCESS_TOKEN
from .contact_parser import Contact

logger = logging.getLogger(__name__)

_SEARCH_PROPS = ["email", "firstname", "lastname", "company", "hs_lead_source"]


def _client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    client = _client()
    req = PublicObjectSearchRequest(
        filter_groups=[
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        properties=_SEARCH_PROPS,
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if resp.results:
            return resp.results[0].to_dict()
    except ApiException as e:
        logger.error("HubSpot search error for %s: %s", email, e)
    return None


def _needs_update(existing: dict, contact: Contact) -> dict:
    """Return only the properties that are missing in the existing record."""
    props = existing.get("properties", {})
    updates: dict = {}
    if not props.get("firstname") and contact.firstname:
        updates["firstname"] = contact.firstname
    if not props.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not props.get("company") and contact.company:
        updates["company"] = contact.company
    return updates


def create_contact(contact: Contact) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None on error."""
    client = _client()
    props = contact.to_hubspot_props()
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        logger.info("Created contact %s (id=%s)", contact.email, result.id)
        return result.id
    except ApiException as e:
        logger.error("Failed to create %s: %s", contact.email, e)
        return None


def update_contact(contact_id: str, updates: dict) -> bool:
    """Patch an existing HubSpot contact with only the changed fields."""
    if not updates:
        return False
    client = _client()
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        logger.info("Updated contact id=%s with %s", contact_id, list(updates.keys()))
        return True
    except ApiException as e:
        logger.error("Failed to update id=%s: %s", contact_id, e)
        return False


def sync_contact(contact: Contact) -> dict:
    """
    Main sync logic for a single contact.
    Returns {"status": "created"|"updated"|"ignored", "email": ..., "hubspot_id": ...}
    """
    existing = find_contact_by_email(contact.email)

    if existing is None:
        new_id = create_contact(contact)
        return {
            "status": "created" if new_id else "error",
            "email": contact.email,
            "hubspot_id": new_id or "",
        }

    contact_id = existing.get("id", "")
    updates = _needs_update(existing, contact)

    if updates:
        ok = update_contact(contact_id, updates)
        return {
            "status": "updated" if ok else "error",
            "email": contact.email,
            "hubspot_id": contact_id,
        }

    return {"status": "ignored", "email": contact.email, "hubspot_id": contact_id}
