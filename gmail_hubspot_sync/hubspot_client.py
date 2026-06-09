import datetime
from typing import Optional

import requests


class HubSpotClient:
    BASE_URL = "https://api.hubapi.com"

    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return existing contact dict or None if not found."""
        url = f"{self.BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        }
        resp = self._session.post(url, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        url = f"{self.BASE_URL}/crm/v3/objects/contacts"
        resp = self._session.post(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{self.BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = self._session.patch(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline / notes
    # ------------------------------------------------------------------

    def create_note(self, contact_id: str, body: str) -> dict:
        """Attach a note to the contact's activity timeline."""
        url = f"{self.BASE_URL}/crm/v3/objects/notes"
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": timestamp,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,
                        }
                    ],
                }
            ],
        }
        resp = self._session.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
