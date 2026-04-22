import logging
import time
from typing import Optional

import requests

from . import config

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.HUBSPOT_TOKEN}",
        "Content-Type": "application/json",
    }


def _raise_for_status(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        logger.error(
            "HubSpot API error %s: %s",
            response.status_code,
            response.text[:300],
        )
        raise exc


# ---------------------------------------------------------------------------
# Contact operations
# ---------------------------------------------------------------------------

def search_contact_by_email(email: str) -> Optional[dict]:
    """Return the first HubSpot contact matching the given email, or None."""
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company"],
        "limit": 1,
    }
    response = requests.post(
        f"{_BASE}/crm/v3/objects/contacts/search",
        json=payload,
        headers=_headers(),
    )
    _raise_for_status(response)
    results = response.json().get("results", [])
    return results[0] if results else None


def create_contact(properties: dict) -> dict:
    """Create a new HubSpot contact and return the created object."""
    response = requests.post(
        f"{_BASE}/crm/v3/objects/contacts",
        json={"properties": properties},
        headers=_headers(),
    )
    _raise_for_status(response)
    return response.json()


def update_contact(contact_id: str, properties: dict) -> dict:
    """Patch an existing HubSpot contact with the given properties."""
    response = requests.patch(
        f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
        json={"properties": properties},
        headers=_headers(),
    )
    _raise_for_status(response)
    return response.json()


# ---------------------------------------------------------------------------
# Timeline / Note operations
# ---------------------------------------------------------------------------

def create_note(body: str, contact_id: str) -> Optional[str]:
    """
    Create a NOTE in HubSpot and associate it with a contact.
    Returns the note ID on success, None on failure (soft-fail).
    """
    timestamp_ms = str(int(time.time() * 1000))

    note_resp = requests.post(
        f"{_BASE}/crm/v3/objects/notes",
        json={"properties": {"hs_note_body": body, "hs_timestamp": timestamp_ms}},
        headers=_headers(),
    )
    try:
        _raise_for_status(note_resp)
    except requests.HTTPError:
        logger.warning("Impossibile creare la nota per il contatto %s", contact_id)
        return None

    note_id = note_resp.json()["id"]

    # Associate note → contact
    assoc_resp = requests.put(
        f"{_BASE}/crm/v3/objects/notes/{note_id}/associations/contacts/{contact_id}/note_to_contact",
        headers=_headers(),
    )
    if not assoc_resp.ok:
        logger.warning(
            "Nota %s creata ma associazione fallita (contatto %s): %s",
            note_id, contact_id, assoc_resp.text[:200],
        )

    return note_id
