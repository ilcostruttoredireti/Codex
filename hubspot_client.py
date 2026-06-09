import time
import logging
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACTS_URL = f"{_BASE}/crm/v3/objects/contacts"
_NOTES_URL = f"{_BASE}/crm/v3/objects/notes"
_SEARCH_URL = f"{_CONTACTS_URL}/search"

# Association type ID: note → contact (HubSpot-defined)
_NOTE_TO_CONTACT_ASSOC_TYPE = 202


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        })

    def find_contact_by_email(self, email: str) -> Optional[Dict]:
        """Return the first contact matching `email`, or None."""
        payload = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        }
        resp = self._session.post(_SEARCH_URL, json=payload)
        resp.raise_for_status()
        results = resp.json().get('results', [])
        return results[0] if results else None

    def create_contact(self, properties: Dict) -> Dict:
        resp = self._session.post(_CONTACTS_URL, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: Dict) -> Dict:
        resp = self._session.patch(
            f"{_CONTACTS_URL}/{contact_id}",
            json={"properties": properties},
        )
        resp.raise_for_status()
        return resp.json()

    def create_note(self, contact_id: str, body: str) -> Optional[Dict]:
        """Attach a note engagement to a contact."""
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [{
                "to": {"id": contact_id},
                "types": [{
                    "associationCategory": "HUBSPOT_DEFINED",
                    "associationTypeId": _NOTE_TO_CONTACT_ASSOC_TYPE,
                }],
            }],
        }
        resp = self._session.post(_NOTES_URL, json=payload)
        if resp.ok:
            return resp.json()
        logger.warning(f"Note creation failed for contact {contact_id}: {resp.status_code} {resp.text[:200]}")
        return None
