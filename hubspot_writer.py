"""HubSpot CRM writer — contacts and notes via REST API v3."""

import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# Association type: Note → Contact (HubSpot built-in)
_NOTE_TO_CONTACT_TYPE_ID = 202


class HubSpotWriter:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs) -> Optional[Dict]:
        try:
            r = self._session.get(f"{_BASE}{path}", timeout=15, **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            logger.error("HubSpot GET %s → %s: %s", path, exc.response.status_code, exc.response.text[:300])
        except Exception as exc:
            logger.error("HubSpot GET %s error: %s", path, exc)
        return None

    def _post(self, path: str, body: Dict) -> Optional[Dict]:
        try:
            r = self._session.post(f"{_BASE}{path}", json=body, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            logger.error("HubSpot POST %s → %s: %s", path, exc.response.status_code, exc.response.text[:300])
        except Exception as exc:
            logger.error("HubSpot POST %s error: %s", path, exc)
        return None

    def _patch(self, path: str, body: Dict) -> Optional[Dict]:
        try:
            r = self._session.patch(f"{_BASE}{path}", json=body, timeout=15)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as exc:
            logger.error("HubSpot PATCH %s → %s: %s", path, exc.response.status_code, exc.response.text[:300])
        except Exception as exc:
            logger.error("HubSpot PATCH %s error: %s", path, exc)
        return None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[Dict]:
        """Return the first HubSpot contact matching *email*, or None."""
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "lifecyclestage"],
            "limit": 1,
        }
        data = self._post("/crm/v3/objects/contacts/search", body)
        if data and data.get("total", 0) > 0:
            return data["results"][0]
        return None

    def create_contact(self, properties: Dict[str, str]) -> Optional[str]:
        """Create a contact. Returns the new ID or None on failure."""
        data = self._post("/crm/v3/objects/contacts", {"properties": properties})
        return data["id"] if data else None

    def update_contact(self, contact_id: str, properties: Dict[str, str]) -> bool:
        """Update an existing contact. Returns True on success."""
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties}) is not None

    def create_note(self, contact_id: str, body_text: str) -> Optional[str]:
        """Create a note associated with *contact_id*. Returns the note ID or None."""
        payload = {
            "properties": {
                "hs_note_body": body_text,
                "hs_timestamp": str(int(time.time() * 1000)),
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
        data = self._post("/crm/v3/objects/notes", payload)
        return data["id"] if data else None
