import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = 'https://api.hubapi.com'
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> Optional[dict]:
        url = f'{_BASE}{path}'
        for attempt in range(4):
            try:
                resp = requests.request(method, url, headers=self._headers, **kwargs)
                if resp.status_code in _RETRY_STATUSES:
                    wait = 2 ** attempt
                    logger.warning(f"HubSpot {resp.status_code}, ritento tra {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                if resp.content:
                    return resp.json()
                return {}
            except requests.HTTPError as e:
                logger.error(f"HubSpot HTTP error {method} {path}: {e}")
                return None
            except requests.RequestException as e:
                logger.error(f"HubSpot request error: {e}")
                return None
        logger.error(f"HubSpot: tentativi esauriti per {path}")
        return None

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing contact dict or None."""
        payload = {
            'filterGroups': [{
                'filters': [{
                    'propertyName': 'email',
                    'operator': 'EQ',
                    'value': email,
                }]
            }],
            'properties': ['email', 'firstname', 'lastname', 'company'],
            'limit': 1,
        }
        data = self._request('POST', '/crm/v3/objects/contacts/search', json=payload)
        if data and data.get('total', 0) > 0:
            return data['results'][0]
        return None

    def create_contact(self, properties: dict) -> Optional[str]:
        """Create a contact and return its ID, or None on failure."""
        data = self._request('POST', '/crm/v3/objects/contacts', json={'properties': properties})
        return data.get('id') if data else None

    def update_contact(self, contact_id: str, properties: dict) -> bool:
        """Patch a contact with the given properties. Returns True on success."""
        if not properties:
            return True
        result = self._request(
            'PATCH', f'/crm/v3/objects/contacts/{contact_id}',
            json={'properties': properties},
        )
        return result is not None

    # ------------------------------------------------------------------
    # Notes (used for "Inbound Gmail" tag)
    # ------------------------------------------------------------------

    def add_note_to_contact(self, contact_id: str, body: str) -> Optional[str]:
        """Create a note and associate it with a contact. Returns note ID or None."""
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        payload = {
            'properties': {
                'hs_note_body': body,
                'hs_timestamp': timestamp,
            },
            'associations': [{
                'to': {'id': contact_id},
                'types': [{
                    'associationCategory': 'HUBSPOT_DEFINED',
                    'associationTypeId': 202,  # note → contact
                }],
            }],
        }
        data = self._request('POST', '/crm/v3/objects/notes', json=payload)
        return data.get('id') if data else None
