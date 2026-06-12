"""HubSpot CRM API client (Private App token)."""

import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_NOTE_TO_CONTACT_TYPE_ID = 202  # HubSpot defined association type


def _headers() -> dict:
    token = os.environ["HUBSPOT_TOKEN"]
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _raise_for_status(resp: requests.Response, action: str) -> None:
    if not resp.ok:
        log.error("HubSpot %s failed %s: %s", action, resp.status_code, resp.text[:300])
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Contact search / create / update
# ---------------------------------------------------------------------------

def find_contact_by_email(email: str) -> dict | None:
    """Return the first matching contact record or None."""
    url = f"{_BASE}/crm/v3/objects/contacts/search"
    body = {
        "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
        "properties": ["email", "firstname", "lastname", "company", "leadsource"],
        "limit": 1,
    }
    resp = requests.post(url, headers=_headers(), json=body)
    _raise_for_status(resp, "search_contact")
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(props: dict) -> dict:
    url = f"{_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, headers=_headers(), json={"properties": props})
    _raise_for_status(resp, "create_contact")
    return resp.json()


def update_contact(contact_id: str, props: dict) -> dict:
    url = f"{_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, headers=_headers(), json={"properties": props})
    _raise_for_status(resp, "update_contact")
    return resp.json()


# ---------------------------------------------------------------------------
# Notes / timeline activity
# ---------------------------------------------------------------------------

def create_note(contact_id: str, body_text: str) -> str | None:
    """Create a note associated to a contact. Returns the note id or None."""
    url = f"{_BASE}/crm/v3/objects/notes"
    now_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    payload = {
        "properties": {
            "hs_note_body": body_text,
            "hs_timestamp": now_ms,
        },
        "associations": [
            {
                "to": {"id": str(contact_id)},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
                    }
                ],
            }
        ],
    }
    resp = requests.post(url, headers=_headers(), json=payload)
    if not resp.ok:
        log.warning("Impossibile creare nota per contatto %s: %s", contact_id, resp.text[:200])
        return None
    return resp.json().get("id")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_contact_props(sender: dict, source_label: str = "Gmail") -> dict:
    """Build the HubSpot property dict from parsed sender data."""
    props: dict = {"email": sender["email"], "leadsource": source_label}
    if sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if sender.get("company"):
        props["company"] = sender["company"]
    return props


def missing_props(existing_record: dict, candidate: dict) -> dict:
    """Return only fields in candidate that are blank in the existing record."""
    current = existing_record.get("properties", {})
    return {k: v for k, v in candidate.items() if v and not current.get(k)}
