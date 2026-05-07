import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = 'https://api.hubapi.com'


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        })

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f'{BASE_URL}{path}'
        resp = self._session.request(method, url, **kwargs)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get('Retry-After', 10))
            logger.warning("HubSpot rate limit hit, waiting %ds", retry_after)
            time.sleep(retry_after)
            resp = self._session.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact record or None."""
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
        try:
            result = self._request('POST', '/crm/v3/objects/contacts/search', json=payload)
            results = result.get('results', [])
            return results[0] if results else None
        except requests.HTTPError as exc:
            logger.error("Search failed for %s: %s", email, exc)
            return None

    def create_contact(self, properties: dict) -> dict:
        return self._request('POST', '/crm/v3/objects/contacts', json={'properties': properties})

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        return self._request(
            'PATCH', f'/crm/v3/objects/contacts/{contact_id}', json={'properties': properties}
        )

    # ------------------------------------------------------------------
    # Timeline / notes
    # ------------------------------------------------------------------

    def add_note_to_contact(self, contact_id: str, body: str) -> dict:
        """Attach a plain-text note to a contact."""
        payload = {
            'properties': {
                'hs_note_body': body,
                'hs_timestamp': str(int(time.time() * 1000)),
            },
            'associations': [{
                'to': {'id': contact_id},
                'types': [{
                    'associationCategory': 'HUBSPOT_DEFINED',
                    'associationTypeId': 202,   # Note → Contact
                }],
            }],
        }
        return self._request('POST', '/crm/v3/objects/notes', json=payload)
