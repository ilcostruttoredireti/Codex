"""HubSpot CRM API client: contacts search, create, update, notes."""

import logging
import time
from typing import Dict, List, Optional

import requests

from .config import HUBSPOT_ACCESS_TOKEN

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_TIMEOUT = 15

# HubSpot association type: Note → Contact (HUBSPOT_DEFINED id 202)
_NOTE_TO_CONTACT_TYPE_ID = 202


def _headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _safe_post(url: str, payload: Dict, label: str) -> Optional[Dict]:
    try:
        r = requests.post(url, json=payload, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as exc:
        logger.error(f"HubSpot {label} HTTP {exc.response.status_code}: {exc.response.text[:300]}")
    except requests.RequestException as exc:
        logger.error(f"HubSpot {label} request error: {exc}")
    return None


def _safe_patch(url: str, payload: Dict, label: str) -> Optional[Dict]:
    try:
        r = requests.patch(url, json=payload, headers=_headers(), timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as exc:
        logger.error(f"HubSpot {label} HTTP {exc.response.status_code}: {exc.response.text[:300]}")
    except requests.RequestException as exc:
        logger.error(f"HubSpot {label} request error: {exc}")
    return None


def search_contact_by_email(email: str) -> Optional[Dict]:
    """Search HubSpot contacts by email. Returns the first match or None."""
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
    data = _safe_post(f"{_BASE}/crm/v3/objects/contacts/search", payload, "search_contact")
    if data:
        results = data.get("results", [])
        return results[0] if results else None
    return None


def create_contact(properties: Dict[str, str]) -> Optional[Dict]:
    """Create a new HubSpot contact. Returns the created object or None."""
    return _safe_post(
        f"{_BASE}/crm/v3/objects/contacts",
        {"properties": properties},
        "create_contact",
    )


def update_contact(contact_id: str, properties: Dict[str, str]) -> Optional[Dict]:
    """Patch an existing HubSpot contact with the given properties."""
    return _safe_patch(
        f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
        {"properties": properties},
        f"update_contact({contact_id})",
    )


def create_note(contact_id: str, body: str, timestamp_ms: int = 0) -> Optional[Dict]:
    """
    Create a HubSpot Note and associate it with a contact.
    timestamp_ms: epoch milliseconds for the note timestamp (0 = now).
    """
    ts = timestamp_ms if timestamp_ms > 0 else int(time.time() * 1000)
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(ts),
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
                    }
                ],
            }
        ],
    }
    return _safe_post(f"{_BASE}/crm/v3/objects/notes", payload, f"create_note({contact_id})")
