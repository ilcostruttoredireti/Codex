"""HubSpot CRM client — create and update contacts."""

import os
from dataclasses import dataclass
from typing import Optional

import requests

_BASE = "https://api.hubapi.com"
_CONTACTS_ENDPOINT = f"{_BASE}/crm/v3/objects/contacts"
_SEARCH_ENDPOINT = f"{_CONTACTS_ENDPOINT}/search"

INBOUND_NOTE = "Inbound Gmail — contact synced from Gmail inbox"


def _token() -> str:
    token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    if not token:
        raise EnvironmentError("HUBSPOT_ACCESS_TOKEN env var not set")
    return token


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json",
    }


@dataclass
class HubSpotContact:
    id: str
    email: str
    first_name: str
    last_name: str
    company: str


def find_contact_by_email(email: str) -> Optional[HubSpotContact]:
    """Return an existing HubSpot contact matching email, or None."""
    payload = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
        "limit": 1,
    }
    resp = requests.post(_SEARCH_ENDPOINT, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    results = data.get("results", [])
    if not results:
        return None
    r = results[0]
    props = r.get("properties", {})
    return HubSpotContact(
        id=r["id"],
        email=props.get("email", email),
        first_name=props.get("firstname", ""),
        last_name=props.get("lastname", ""),
        company=props.get("company", ""),
    )


def _build_properties(contact_info, existing: Optional[HubSpotContact] = None) -> dict:
    """Build the properties dict for create / update calls (writable fields only)."""
    props: dict = {}

    if existing is None:
        props["email"] = contact_info.email
        if contact_info.first_name:
            props["firstname"] = contact_info.first_name
        if contact_info.last_name:
            props["lastname"] = contact_info.last_name
        if contact_info.company:
            props["company"] = contact_info.company
    else:
        # Only patch truly missing fields — never overwrite existing data
        if not existing.first_name and contact_info.first_name:
            props["firstname"] = contact_info.first_name
        if not existing.last_name and contact_info.last_name:
            props["lastname"] = contact_info.last_name
        if not existing.company and contact_info.company:
            props["company"] = contact_info.company

    return props


def create_contact(contact_info) -> str:
    """Create a new HubSpot contact. Returns the new contact ID."""
    props = _build_properties(contact_info)
    resp = requests.post(_CONTACTS_ENDPOINT, json={"properties": props}, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]


def update_contact(contact_id: str, contact_info, existing: HubSpotContact) -> bool:
    """
    Update an existing contact with any missing fields.
    Returns True if any field was actually updated.
    """
    props = _build_properties(contact_info, existing)
    if not props:
        return False
    url = f"{_CONTACTS_ENDPOINT}/{contact_id}"
    resp = requests.patch(url, json={"properties": props}, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return True


def create_note(contact_id: str, body: str = INBOUND_NOTE) -> str:
    """Create a HubSpot Note and associate it with a contact. Returns note ID."""
    note_endpoint = f"{_BASE}/crm/v3/objects/notes"
    payload = {
        "properties": {"hs_note_body": body, "hs_timestamp": "now"},
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            }
        ],
    }
    resp = requests.post(note_endpoint, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()["id"]
