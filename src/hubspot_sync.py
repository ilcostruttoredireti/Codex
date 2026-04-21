import os
import time
import logging
import requests

log = logging.getLogger(__name__)

_BASE = 'https://api.hubapi.com'
# HubSpot association type: Note → Contact (categoria HUBSPOT_DEFINED)
_NOTE_TO_CONTACT = 202


class HubSpotSync:
    def __init__(self, access_token=None):
        self.token = access_token or os.environ.get('HUBSPOT_ACCESS_TOKEN')
        if not self.token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN non impostato.")
        self._headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }

    # ------------------------------------------------------------------ #
    #  API pubblica                                                         #
    # ------------------------------------------------------------------ #

    def sync_contact(self, contact_data):
        email = contact_data['email']
        existing = self._search(email)

        if existing:
            contact_id = existing['id']
            updated = self._fill_missing(contact_id, existing['properties'], contact_data)
            self._add_note(contact_id, contact_data)
            status = 'Aggiornato' if updated else 'Ignorato'
        else:
            contact_id = self._create(contact_data)
            status = 'Creato' if contact_id else 'Errore'
            if contact_id:
                self._add_note(contact_id, contact_data)

        return {'status': status, 'email': email, 'id': contact_id}

    # ------------------------------------------------------------------ #
    #  Metodi interni                                                       #
    # ------------------------------------------------------------------ #

    def _search(self, email):
        url = f'{_BASE}/crm/v3/objects/contacts/search'
        payload = {
            'filterGroups': [{'filters': [
                {'propertyName': 'email', 'operator': 'EQ', 'value': email}
            ]}],
            'properties': ['email', 'firstname', 'lastname', 'company'],
            'limit': 1,
        }
        r = self._post(url, payload)
        if r and r.status_code == 200:
            data = r.json()
            if data.get('total', 0) > 0:
                c = data['results'][0]
                return {'id': c['id'], 'properties': c.get('properties', {})}
        return None

    def _create(self, data):
        url = f'{_BASE}/crm/v3/objects/contacts'
        props = {'email': data['email'], 'lifecyclestage': 'lead'}
        if data.get('first_name'):
            props['firstname'] = data['first_name']
        if data.get('last_name'):
            props['lastname'] = data['last_name']
        if data.get('company'):
            props['company'] = data['company']

        r = self._post(url, {'properties': props})
        if r and r.status_code == 201:
            return r.json()['id']
        if r:
            log.error(f"Creazione contatto fallita: {r.status_code} {r.text}")
        return None

    def _fill_missing(self, contact_id, existing, new_data):
        updates = {}
        if new_data.get('first_name') and not existing.get('firstname'):
            updates['firstname'] = new_data['first_name']
        if new_data.get('last_name') and not existing.get('lastname'):
            updates['lastname'] = new_data['last_name']
        if new_data.get('company') and not existing.get('company'):
            updates['company'] = new_data['company']
        if not updates:
            return False

        url = f'{_BASE}/crm/v3/objects/contacts/{contact_id}'
        r = requests.patch(url, json={'properties': updates}, headers=self._headers, timeout=15)
        if r.status_code == 200:
            log.debug(f"Campi aggiornati per {contact_id}: {list(updates)}")
            return True
        log.error(f"Aggiornamento contatto fallito: {r.status_code} {r.text}")
        return False

    def _add_note(self, contact_id, data):
        url = f'{_BASE}/crm/v3/objects/notes'
        body = (
            f"Email ricevuta via Gmail\n"
            f"Oggetto: {data.get('subject') or 'N/A'}\n"
            f"Data: {data.get('date') or 'N/A'}\n"
            f"Fonte: Inbound Gmail"
        )
        payload = {
            'properties': {
                'hs_note_body': body,
                'hs_timestamp': str(int(time.time() * 1000)),
            },
            'associations': [{
                'to': {'id': contact_id},
                'types': [{'associationCategory': 'HUBSPOT_DEFINED', 'associationTypeId': _NOTE_TO_CONTACT}],
            }],
        }
        r = self._post(url, payload)
        if not r or r.status_code != 201:
            status = r.status_code if r else 'N/A'
            log.warning(f"Nota non aggiunta al contatto {contact_id}: {status}")

    # ------------------------------------------------------------------ #
    #  Helper HTTP con gestione rate-limit                                  #
    # ------------------------------------------------------------------ #

    def _post(self, url, payload, retries=3):
        for attempt in range(retries):
            try:
                r = requests.post(url, json=payload, headers=self._headers, timeout=15)
                if r.status_code == 429:
                    wait = int(r.headers.get('Retry-After', 2 ** (attempt + 1)))
                    log.warning(f"Rate limit HubSpot, attendo {wait}s...")
                    time.sleep(wait)
                    continue
                return r
            except requests.RequestException as e:
                log.error(f"Errore di rete HubSpot (tentativo {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        return None
