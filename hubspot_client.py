"""HubSpot client: contact lookup, create, update, and timeline activity."""

from datetime import datetime, timezone
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

from utils import company_from_domain, is_no_reply

SOURCE_LABEL = "Gmail"
INBOUND_TAG = "Inbound Gmail"


def build_hubspot_client(access_token: str) -> hubspot.Client:
    return hubspot.Client.create(access_token=access_token)


def search_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching *email*, or None."""
    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_source",
                    "notes_last_updated"],
    )
    try:
        result = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if result.total > 0:
            return result.results[0]
    except ApiException as exc:
        print(f"  [HubSpot] search error: {exc}")
    return None


def _build_properties(sender: dict, existing: Optional[dict] = None) -> dict:
    """
    Build a HubSpot properties dict from sender data.
    When *existing* is provided, only fill in missing / empty fields.
    """
    props = {}
    existing_props = existing.properties if existing else {}

    def _missing(key: str) -> bool:
        val = existing_props.get(key, "")
        return not val or val.strip() == ""

    if _missing("email"):
        props["email"] = sender["email"]

    if sender.get("first_name") and _missing("firstname"):
        props["firstname"] = sender["first_name"]

    if sender.get("last_name") and _missing("lastname"):
        props["lastname"] = sender["last_name"]

    company_guess = company_from_domain(sender.get("domain", ""))
    if company_guess and _missing("company"):
        props["company"] = company_guess

    if _missing("hs_lead_source"):
        props["hs_lead_source"] = SOURCE_LABEL

    return props


def create_contact(client: hubspot.Client, sender: dict) -> dict:
    """Create a new HubSpot contact. Returns {'status', 'contact_id', 'email'}."""
    props = _build_properties(sender)
    props["email"] = sender["email"]  # always set email on create
    body = SimplePublicObjectInput(properties=props)
    try:
        contact = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=body
        )
        _add_timeline_note(client, contact.id, sender)
        return {"status": "Creato", "contact_id": contact.id, "email": sender["email"]}
    except ApiException as exc:
        return {"status": f"Errore creazione: {exc}", "contact_id": None, "email": sender["email"]}


def update_contact(client: hubspot.Client, existing, sender: dict) -> dict:
    """Update existing contact with any missing fields. Returns {'status', 'contact_id', 'email'}."""
    props = _build_properties(sender, existing)
    contact_id = existing.id

    if props:
        body = SimplePublicObjectInput(properties=props)
        try:
            client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=body,
            )
        except ApiException as exc:
            return {"status": f"Errore aggiornamento: {exc}", "contact_id": contact_id, "email": sender["email"]}

    _add_timeline_note(client, contact_id, sender)
    return {"status": "Aggiornato", "contact_id": contact_id, "email": sender["email"]}


def _add_timeline_note(client: hubspot.Client, contact_id: str, sender: dict):
    """Associate a note (engagement) with the contact recording the inbound email."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = {
        "engagement": {
            "active": True,
            "type": "NOTE",
            "timestamp": now_ms,
        },
        "associations": {
            "contactIds": [int(contact_id)],
        },
        "metadata": {
            "body": (
                f"Email in entrata ricevuta da {sender['email']}.\n"
                f"Tag: {INBOUND_TAG}\n"
                f"Fonte: {SOURCE_LABEL}"
            ),
        },
    }
    try:
        client.api_client.call_api(
            "/engagements/v1/engagements",
            "POST",
            body=body,
            response_type=object,
            auth_settings=["hapikey", "oauth2"],
        )
    except Exception:
        # Timeline note is best-effort; do not fail the whole sync.
        pass


def sync_contact(client: hubspot.Client, sender: dict) -> dict:
    """
    Main entry point: search for the sender's contact in HubSpot,
    create or update it, and return a result dict.

    Result keys: status, contact_id, email
    """
    email = sender.get("email", "").lower().strip()
    if not email:
        return {"status": "Ignorato (no email)", "contact_id": None, "email": ""}

    if is_no_reply(email):
        return {"status": "Ignorato (no-reply)", "contact_id": None, "email": email}

    existing = search_contact_by_email(client, email)
    if existing:
        return update_contact(client, existing, sender)
    return create_contact(client, sender)
