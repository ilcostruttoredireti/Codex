"""HubSpot CRM wrapper — find, create, and update contacts."""
from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)
from hubspot.crm.contacts.exceptions import ApiException

from . import config
from .contact_parser import Contact


def _client() -> HubSpot:
    return HubSpot(access_token=config.HUBSPOT_ACCESS_TOKEN)


def find_contact_by_email(email: str) -> dict | None:
    """Return the HubSpot contact record dict or None."""
    api = _client().crm.contacts
    search_req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[Filter(property_name="email", operator="EQ", value=email.lower())]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    try:
        results = api.search_api.do_search(public_object_search_request=search_req)
        if results.results:
            r = results.results[0]
            return {"id": r.id, "properties": r.properties}
    except ApiException:
        pass
    return None


def _build_properties(contact: Contact, existing: dict | None = None) -> dict:
    props: dict[str, str] = {}
    existing_props = (existing or {}).get("properties", {})

    if contact.first_name and not existing_props.get("firstname"):
        props["firstname"] = contact.first_name
    if contact.last_name and not existing_props.get("lastname"):
        props["lastname"] = contact.last_name
    if contact.company and not existing_props.get("company"):
        props["company"] = contact.company
    if not existing_props.get("hs_lead_source"):
        props["hs_lead_source"] = "OTHER"  # HubSpot enum closest to "Gmail"

    return props


def upsert_contact(contact: Contact) -> tuple[str, str]:
    """
    Create or update a HubSpot contact.
    Returns (status, hubspot_id) where status is 'Creato', 'Aggiornato', or 'Ignorato'.
    """
    api = _client().crm.contacts
    existing = find_contact_by_email(contact.email)

    if existing:
        props = _build_properties(contact, existing)
        if not props:
            return "Ignorato", existing["id"]
        try:
            api.basic_api.update(
                contact_id=existing["id"],
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            return "Aggiornato", existing["id"]
        except ApiException:
            return "Ignorato", existing["id"]
    else:
        props = {
            "email": contact.email,
            "hs_lead_source": "OTHER",
        }
        if contact.first_name:
            props["firstname"] = contact.first_name
        if contact.last_name:
            props["lastname"] = contact.last_name
        if contact.company:
            props["company"] = contact.company
        try:
            result = api.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return "Creato", result.id
        except ApiException as e:
            raise RuntimeError(f"Failed to create {contact.email}: {e}") from e
