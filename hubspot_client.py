import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = 'https://api.hubapi.com'


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update({
            'Authorization': f'Bearer {access_token}',
            'Content-Type': 'application/json',
        })

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the contact dict if found, otherwise None."""
        payload = {
            'filterGroups': [{
                'filters': [{
                    'propertyName': 'email',
                    'operator': 'EQ',
                    'value': email.lower(),
                }]
            }],
            'properties': ['email', 'firstname', 'lastname', 'company'],
            'limit': 1,
        }
        result = self._req('POST', f'{_BASE}/crm/v3/objects/contacts/search', json=payload)
        results = result.get('results', [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        payload = {'properties': properties}
        return self._req('POST', f'{_BASE}/crm/v3/objects/contacts', json=payload)

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        payload = {'properties': properties}
        return self._req('PATCH', f'{_BASE}/crm/v3/objects/contacts/{contact_id}', json=payload)

    # ------------------------------------------------------------------
    # Timeline / Notes
    # ------------------------------------------------------------------

    def add_note(self, contact_id: str, body: str, timestamp_ms: Optional[int] = None) -> dict:
        """Create a note and associate it with the contact."""
        ts = str(timestamp_ms or int(time.time() * 1000))
        note = self._req('POST', f'{_BASE}/crm/v3/objects/notes', json={
            'properties': {
                'hs_note_body': body,
                'hs_timestamp': ts,
            }
        })
        note_id = note['id']

        # Associate note → contact (HubSpot association type 202 = note-to-contact)
        self._req(
            'PUT',
            f'{_BASE}/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}',
            json=[{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': 202}],
        )
        return note

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _req(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(4):
            resp = self._session.request(method, url, timeout=30, **kwargs)

            if resp.status_code == 429:
                wait = int(resp.headers.get('Retry-After', 10))
                logger.warning("HubSpot rate limit – waiting %ds", wait)
                time.sleep(wait)
                continue

            resp.raise_for_status()
            return resp.json() if resp.content else {}

        raise RuntimeError(f"HubSpot request failed after retries: {method} {url}")
