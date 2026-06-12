"""HubSpot API client — wraps hubspot-api-client."""
from __future__ import annotations

import os
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)

_HS_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# hs_analytics_source is the writable "Original Traffic Source" field.
# hs_analytics_source_data_* are read-only (set by HubSpot tracking).
CONTACT_ANALYTICS_SOURCE = "EMAIL_MARKETING"


def _client() -> hubspot.Client:
    return hubspot.Client.create(access_token=_HS_TOKEN)


def find_contact_by_email(email: str) -> Optional[dict]:
    """Return HubSpot contact dict or None if not found."""
    client = _client()
    try:
        from hubspot.crm.contacts import PublicObjectSearchRequest
        search_req = PublicObjectSearchRequest(
            filter_groups=[{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            properties=["email", "firstname", "lastname", "company",
                        "hs_analytics_source"],
        )
        response = client.crm.contacts.search_api.do_search(search_req)
        if response.total > 0:
            return response.results[0].to_dict()
    except ApiException:
        pass
    return None


def create_contact(email: str, first_name: Optional[str], last_name: Optional[str],
                   company: Optional[str]) -> dict:
    """Create a new HubSpot contact and return the created object dict."""
    client = _client()
    props: dict[str, str] = {
        "email": email,
        "hs_analytics_source": CONTACT_ANALYTICS_SOURCE,
    }
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company

    body = SimplePublicObjectInputForCreate(properties=props)
    result = client.crm.contacts.basic_api.create(simple_public_object_input_for_create=body)
    return result.to_dict()


def update_contact(contact_id: str, first_name: Optional[str], last_name: Optional[str],
                   company: Optional[str], existing: dict) -> dict:
    """Patch only fields that are currently blank."""
    client = _client()
    existing_props = existing.get("properties", {})
    props: dict[str, str] = {}

    if first_name and not existing_props.get("firstname"):
        props["firstname"] = first_name
    if last_name and not existing_props.get("lastname"):
        props["lastname"] = last_name
    if company and not existing_props.get("company"):
        props["company"] = company
    if not existing_props.get("hs_analytics_source"):
        props["hs_analytics_source"] = CONTACT_ANALYTICS_SOURCE

    if not props:
        return existing

    body = SimplePublicObjectInput(properties=props)
    result = client.crm.contacts.basic_api.update(
        contact_id=contact_id, simple_public_object_input=body
    )
    return result.to_dict()


def log_email_activity(contact_id: str, subject: str, snippet: str, date: str) -> None:
    """Create a Note on the contact to log the inbound Gmail email."""
    client = _client()
    try:
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
        body = NoteCreate(
            properties={
                "hs_note_body": (
                    f"📧 Email ricevuta via Gmail [Inbound Gmail]\n"
                    f"Oggetto: {subject}\n"
                    f"{snippet}\n"
                    f"Fonte: Gmail Inbound"
                ),
                "hs_timestamp": _iso_to_ms(date),
            },
            associations=[{
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED",
                           "associationTypeId": 202}],
            }],
        )
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=body
        )
    except Exception:
        pass  # activity logging is best-effort


def _iso_to_ms(date_str: str) -> str:
    """Convert RFC 2822 / ISO date string to milliseconds epoch string."""
    from email.utils import parsedate_to_datetime
    import calendar
    try:
        dt = parsedate_to_datetime(date_str)
        return str(int(calendar.timegm(dt.utctimetuple())) * 1000)
    except Exception:
        from datetime import datetime, timezone
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))
