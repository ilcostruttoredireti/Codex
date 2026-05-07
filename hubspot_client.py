import logging
import requests

log = logging.getLogger(__name__)

HUBSPOT_BASE = 'https://api.hubapi.com'


class HubSpotClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json',
        })

    def find_contact_by_email(self, email: str) -> dict | None:
        """Search HubSpot contacts by email. Returns the first match or None."""
        url = f'{HUBSPOT_BASE}/crm/v3/objects/contacts/search'
        payload = {
            'filterGroups': [{
                'filters': [{
                    'propertyName': 'email',
                    'operator': 'EQ',
                    'value': email.lower(),
                }]
            }],
            'properties': ['email', 'firstname', 'lastname', 'company', 'lead_source'],
            'limit': 1,
        }
        r = self.session.post(url, json=payload)
        if r.status_code == 200:
            results = r.json().get('results', [])
            return results[0] if results else None
        log.error(f"HubSpot search error {r.status_code}: {r.text[:300]}")
        return None

    def create_contact(self, properties: dict) -> str | None:
        """Create a new contact. Returns the new contact ID or None on failure."""
        url = f'{HUBSPOT_BASE}/crm/v3/objects/contacts'
        r = self.session.post(url, json={'properties': properties})
        if r.status_code in (200, 201):
            return r.json().get('id')
        log.error(f"HubSpot create contact error {r.status_code}: {r.text[:300]}")
        return None

    def update_contact(self, contact_id: str, properties: dict) -> bool:
        """Patch an existing contact with new property values."""
        url = f'{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}'
        r = self.session.patch(url, json={'properties': properties})
        if r.status_code == 200:
            return True
        log.error(f"HubSpot update contact {contact_id} error {r.status_code}: {r.text[:300]}")
        return False

    def create_note(self, contact_id: str, body: str, timestamp_ms: int | None = None) -> str | None:
        """Create a timeline note and associate it with a contact."""
        import time
        note_props = {
            'hs_note_body': body,
            'hs_timestamp': str(timestamp_ms or int(time.time() * 1000)),
        }
        url = f'{HUBSPOT_BASE}/crm/v3/objects/notes'
        r = self.session.post(url, json={'properties': note_props})
        if r.status_code not in (200, 201):
            log.error(f"HubSpot create note error {r.status_code}: {r.text[:300]}")
            return None

        note_id = r.json().get('id')

        # Associate note → contact
        assoc_url = (
            f'{HUBSPOT_BASE}/crm/v3/objects/notes/{note_id}'
            f'/associations/contacts/{contact_id}/note_to_contact'
        )
        ar = self.session.put(assoc_url)
        if ar.status_code not in (200, 201):
            log.warning(f"Note created ({note_id}) but association failed: {ar.status_code}")

        return note_id
