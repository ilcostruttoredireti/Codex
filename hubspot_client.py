"""HubSpot CRM client: find, create, and update contacts."""

import logging

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

logger = logging.getLogger(__name__)

# HubSpot property used to tag contacts sourced from Gmail
LEAD_SOURCE_VALUE = "Gmail"
GMAIL_TAG_PROPERTY = "hs_lead_source"


def build_client(access_token: str) -> hubspot.Client:
    return hubspot.Client.create(access_token=access_token)


# ---------------------------------------------------------------------------
# Contact lookup
# ---------------------------------------------------------------------------

def find_contact_by_email(client: hubspot.Client, email: str):
    """Return the first matching HubSpot contact or None."""
    search_filter = Filter(property_name="email", operator="EQ", value=email)
    search_request = PublicObjectSearchRequest(
        filter_groups=[FilterGroup(filters=[search_filter])],
        properties=["email", "firstname", "lastname", "company", GMAIL_TAG_PROPERTY],
        limit=1,
    )
    try:
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_request
        )
        if result.total > 0:
            return result.results[0]
    except ApiException as exc:
        logger.error("HubSpot search failed for %s: %s", email, exc)
    return None


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

def create_contact(client: hubspot.Client, sender: dict) -> str | None:
    """
    Create a new HubSpot contact from *sender* data.
    Returns the new contact ID, or None on failure.
    """
    props = {
        "email": sender["email"],
        GMAIL_TAG_PROPERTY: LEAD_SOURCE_VALUE,
    }
    if sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if sender.get("company"):
        props["company"] = sender["company"]

    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        logger.info("Created contact %s (id=%s)", sender["email"], contact.id)
        return contact.id
    except ApiException as exc:
        # 409 = contact already exists (race condition) — treat as existing
        if exc.status == 409:
            logger.warning("Contact %s already exists (race), will re-lookup.", sender["email"])
            existing = find_contact_by_email(client, sender["email"])
            return existing.id if existing else None
        logger.error("HubSpot create failed for %s: %s", sender["email"], exc)
        return None


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------

def update_contact_if_needed(
    client: hubspot.Client,
    contact_id: str,
    existing_props: dict,
    sender: dict,
) -> bool:
    """
    Fill in only the properties that are currently blank.
    Returns True if any update was sent, False if contact was already complete.
    """
    updates: dict[str, str] = {}

    if sender.get("first_name") and not existing_props.get("firstname"):
        updates["firstname"] = sender["first_name"]
    if sender.get("last_name") and not existing_props.get("lastname"):
        updates["lastname"] = sender["last_name"]
    if sender.get("company") and not existing_props.get("company"):
        updates["company"] = sender["company"]
    if not existing_props.get(GMAIL_TAG_PROPERTY):
        updates[GMAIL_TAG_PROPERTY] = LEAD_SOURCE_VALUE

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        logger.info("Updated contact %s (id=%s) fields: %s",
                    sender["email"], contact_id, list(updates.keys()))
        return True
    except ApiException as exc:
        logger.error("HubSpot update failed for contact %s: %s", contact_id, exc)
        return False


# ---------------------------------------------------------------------------
# Unified upsert
# ---------------------------------------------------------------------------

def upsert_contact(client: hubspot.Client, sender: dict) -> dict:
    """
    Create or update a HubSpot contact for *sender*.

    Returns:
        {"status": "Creato"|"Aggiornato"|"Ignorato"|"Errore",
         "email": str,
         "contact_id": str|None}
    """
    email = sender["email"]

    existing = find_contact_by_email(client, email)

    if existing:
        updated = update_contact_if_needed(
            client,
            contact_id=existing.id,
            existing_props=existing.properties or {},
            sender=sender,
        )
        return {
            "status": "Aggiornato" if updated else "Ignorato",
            "email": email,
            "contact_id": existing.id,
        }

    contact_id = create_contact(client, sender)
    return {
        "status": "Creato" if contact_id else "Errore",
        "email": email,
        "contact_id": contact_id,
    }
