"""Thin wrapper around the HubSpot CRM API for contact upsert operations."""

import logging
import os
import time
from typing import Optional

from hubspot import HubSpot
from hubspot.crm.contacts.exceptions import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

logger = logging.getLogger(__name__)

_CONTACT_PROPS = ["email", "firstname", "lastname", "company"]


def get_client() -> HubSpot:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError(
            "HUBSPOT_ACCESS_TOKEN is not set. "
            "Create a Private App in HubSpot Settings → Integrations → Private Apps."
        )
    return HubSpot(access_token=token)


def find_by_email(client: HubSpot, email: str) -> Optional[object]:
    """Search for a HubSpot contact by email. Returns the contact object or None."""
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[Filter(property_name="email", operator="EQ", value=email)])
        ],
        properties=_CONTACT_PROPS,
        limit=1,
    )
    try:
        result = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if result.total > 0:
            return result.results[0]
    except ApiException as exc:
        logger.error("HubSpot search failed for %s: %s", email, exc)
    return None


def create_contact(
    client: HubSpot,
    email: str,
    first_name: Optional[str],
    last_name: Optional[str],
    company: Optional[str],
) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None on failure."""
    props = {"email": email}
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company

    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        return contact.id
    except ApiException as exc:
        if exc.status == 409:
            # Race condition: another thread already created this contact
            logger.debug("Contact %s created concurrently (409), skipping", email)
        else:
            logger.error("HubSpot create failed for %s: %s", email, exc)
    return None


def update_missing_fields(
    client: HubSpot,
    contact_id: str,
    existing_props: dict,
    first_name: Optional[str],
    last_name: Optional[str],
    company: Optional[str],
) -> bool:
    """Patch an existing contact with any fields that are currently empty.

    Returns True if at least one field was updated.
    """
    updates: dict = {}
    if first_name and not existing_props.get("firstname"):
        updates["firstname"] = first_name
    if last_name and not existing_props.get("lastname"):
        updates["lastname"] = last_name
    if company and not existing_props.get("company"):
        updates["company"] = company

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True
    except ApiException as exc:
        logger.error("HubSpot update failed for contact %s: %s", contact_id, exc)
    return False


def create_activity_note(
    access_token: str,
    contact_id: str,
    sender_email: str,
) -> Optional[str]:
    """Create a timeline note on the contact recording the inbound Gmail email.

    Uses the REST API directly to avoid complex SDK association boilerplate.
    Returns the note ID or None on failure.
    """
    import requests  # noqa: PLC0415 — intentional lazy import

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "properties": {
            "hs_timestamp": str(int(time.time() * 1000)),
            "hs_note_body": (
                f"Email inbound ricevuta da: {sender_email}\n"
                "Fonte: Gmail\nTag: Inbound Gmail"
            ),
        },
        "associations": [
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
    }

    try:
        resp = requests.post(
            "https://api.hubapi.com/crm/v3/objects/notes",
            headers=headers,
            json=payload,
            timeout=10,
        )
        if resp.status_code == 201:
            return resp.json().get("id")
        logger.warning(
            "Note creation returned %s: %s", resp.status_code, resp.text[:200]
        )
    except Exception as exc:  # network errors, etc.
        logger.warning("Failed to create activity note for %s: %s", sender_email, exc)
    return None
