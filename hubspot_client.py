import logging
import re
import time
from typing import Optional

import requests

from models import EmailContact

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACT_PROPS = ["email", "firstname", "lastname", "company", "hs_lead_source"]


class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact dict or None."""
        url = f"{_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": _CONTACT_PROPS,
            "limit": 1,
        }
        try:
            resp = self._post(url, payload)
            data = resp.json()
            if data.get("total", 0) > 0:
                return data["results"][0]
        except requests.HTTPError as exc:
            logger.error(f"Errore ricerca contatto {email}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, contact: EmailContact) -> Optional[str]:
        """Create a new contact. Returns the new contact ID, or the existing ID on 409."""
        url = f"{_BASE}/crm/v3/objects/contacts"
        payload = {"properties": _build_props(contact)}
        try:
            resp = self._post(url, payload, expected=(200, 201))
            contact_id = resp.json()["id"]
            logger.info(f"Contatto creato: {contact.email} → ID {contact_id}")
            return contact_id
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 409:
                # Contact already exists — extract the existing ID from the error message
                existing_id = _extract_existing_id(exc.response.json())
                if existing_id:
                    logger.info(f"Contatto già esistente (409): {contact.email} → ID {existing_id}")
                    return existing_id
            logger.error(f"Errore creazione contatto {contact.email}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, updates: dict) -> bool:
        """PATCH only the supplied fields onto an existing contact."""
        url = f"{_BASE}/crm/v3/objects/contacts/{contact_id}"
        try:
            self._patch(url, {"properties": updates})
            logger.info(f"Contatto aggiornato ID {contact_id}: {list(updates.keys())}")
            return True
        except requests.HTTPError as exc:
            logger.error(f"Errore aggiornamento contatto ID {contact_id}: {exc}")
            return False

    # ------------------------------------------------------------------
    # Timeline note
    # ------------------------------------------------------------------

    def create_note(self, contact_id: str, body: str) -> Optional[str]:
        url = f"{_BASE}/crm/v3/objects/notes"
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # Note → Contact
                        }
                    ],
                }
            ],
        }
        try:
            resp = self._post(url, payload, expected=(200, 201))
            note_id = resp.json()["id"]
            logger.debug(f"Nota creata ID {note_id} per contatto {contact_id}")
            return note_id
        except requests.HTTPError as exc:
            logger.warning(f"Nota non creata per contatto {contact_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _post(
        self,
        url: str,
        payload: dict,
        expected: tuple = (200, 201),
    ) -> requests.Response:
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        if resp.status_code not in expected:
            resp.raise_for_status()
        return resp

    def _patch(self, url: str, payload: dict) -> requests.Response:
        resp = requests.patch(url, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _build_props(contact: EmailContact) -> dict:
    props: dict = {"email": contact.email}
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    return props


def _extract_existing_id(error_body: dict) -> Optional[str]:
    """Parse 'Contact already exists. Existing ID: 12345' from HubSpot 409 body."""
    message = error_body.get("message", "")
    match = re.search(r"[Ee]xisting ID[:\s]+(\d+)", message)
    return match.group(1) if match else None
