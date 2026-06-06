"""HubSpot CRM client — upserts contacts from Gmail sender data."""

import logging
from typing import Optional
from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    ApiException,
)
from hubspot.crm.contacts.models import PublicObjectSearchRequest, Filter, FilterGroup

logger = logging.getLogger(__name__)

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"


def build_client(access_token: str) -> HubSpot:
    return HubSpot(access_token=access_token)


def find_contact_by_email(client: HubSpot, email: str) -> Optional[dict]:
    """Return the existing HubSpot contact for this email, or None."""
    search_filter = Filter(
        property_name="email",
        operator="EQ",
        value=email,
    )
    filter_group = FilterGroup(filters=[search_filter])
    request = PublicObjectSearchRequest(
        filter_groups=[filter_group],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    try:
        response = client.crm.contacts.search_api.do_search(
            public_object_search_request=request
        )
        if response.total > 0:
            return response.results[0]
    except ApiException:
        logger.exception("HubSpot search error for email %s", email)
    return None


def create_contact(client: HubSpot, sender: dict, company_name: Optional[str]) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None on failure."""
    props = _build_properties(sender, company_name)
    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        contact_id = result.id
        _add_note(client, contact_id, sender["email"])
        return contact_id
    except ApiException as e:
        # 409 = contact already exists (race condition) — treat as update
        if e.status == 409:
            logger.debug("Contact %s created concurrently, will update.", sender["email"])
            contact = find_contact_by_email(client, sender["email"])
            if contact:
                return update_contact(client, contact.id, sender, company_name)
        logger.exception("Error creating contact %s", sender["email"])
        return None


def update_contact(
    client: HubSpot, contact_id: str, sender: dict, company_name: Optional[str]
) -> Optional[str]:
    """Fill in any missing properties on an existing contact."""
    existing = _fetch_contact_props(client, contact_id)
    if existing is None:
        return None

    updates = {}
    props = _build_properties(sender, company_name)

    for key, value in props.items():
        # Only write fields that are currently blank/missing
        if value and not existing.get(key):
            updates[key] = value

    if updates:
        try:
            from hubspot.crm.contacts import SimplePublicObjectInput

            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            logger.debug("Updated contact %s with %s", contact_id, list(updates.keys()))
        except ApiException:
            logger.exception("Error updating contact %s", contact_id)
            return None

    _add_note(client, contact_id, sender["email"])
    return contact_id


def _fetch_contact_props(client: HubSpot, contact_id: str) -> Optional[dict]:
    try:
        contact = client.crm.contacts.basic_api.get_by_id(
            contact_id=contact_id,
            properties=["email", "firstname", "lastname", "company", "leadsource"],
        )
        return contact.properties or {}
    except ApiException:
        logger.exception("Error fetching contact %s", contact_id)
        return None


def _build_properties(sender: dict, company_name: Optional[str]) -> dict:
    props = {
        "email": sender["email"],
        "leadsource": CONTACT_SOURCE,
        "hs_analytics_source": CONTACT_SOURCE,
    }
    if sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if company_name:
        props["company"] = company_name
    return props


def _add_note(client: HubSpot, contact_id: str, email: str):
    """Attach a timeline note tagging this contact as inbound from Gmail."""
    try:
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate

        note_props = {
            "hs_note_body": f"[{INBOUND_TAG}] Email received from {email}",
            "hs_timestamp": _now_ms(),
        }
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteCreate(properties=note_props)
        )
        # Associate note → contact
        from hubspot.crm.associations import BatchInputPublicObjectId, PublicObjectId

        client.crm.associations.batch_api.create(
            from_object_type="notes",
            to_object_type="contacts",
            batch_input_public_object_id=BatchInputPublicObjectId(
                inputs=[PublicObjectId(id=note.id)]
            ),
        )
        # Proper v4 association
        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type_id=202,
        )
    except Exception:
        # Notes are optional — don't fail the whole sync
        logger.debug("Could not add note for contact %s", contact_id, exc_info=True)


def _now_ms() -> str:
    """Return current UTC time as milliseconds-since-epoch string (HubSpot format)."""
    import time
    return str(int(time.time() * 1000))
