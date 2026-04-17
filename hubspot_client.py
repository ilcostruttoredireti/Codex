import time
import logging
from typing import Optional

import requests

HUBSPOT_API = "https://api.hubapi.com"

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the first matching contact or None."""
        url = f"{HUBSPOT_API}/crm/v3/objects/contacts/search"
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
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        url = f"{HUBSPOT_API}/crm/v3/objects/contacts"
        resp = self.session.post(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{HUBSPOT_API}/crm/v3/objects/contacts/{contact_id}"
        resp = self.session.patch(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Notes (optional timeline activity)
    # ------------------------------------------------------------------

    def create_note(self, contact_id: str, body: str) -> Optional[dict]:
        """Attach a note to a contact. Returns None on failure (non-fatal)."""
        url = f"{HUBSPOT_API}/crm/v3/objects/notes"
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
            resp = self.session.post(url, json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.warning("Could not create note for contact %s: %s", contact_id, exc)
            return None
