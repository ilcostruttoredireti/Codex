import time
from typing import Optional

import requests


class HubSpotError(Exception):
    pass


class HubSpotClient:
    BASE_URL = "https://api.hubapi.com"

    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.BASE_URL}{path}"
        resp = requests.request(method, url, headers=self._headers, **kwargs)
        try:
            resp.raise_for_status()
        except requests.HTTPError as exc:
            body = ""
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise HubSpotError(f"HubSpot {method} {path} → {resp.status_code}: {body}") from exc
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------ #
    # Contacts                                                             #
    # ------------------------------------------------------------------ #

    def search_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the first contact matching the email, or None."""
        data = self._request(
            "POST",
            "/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "email",
                                "operator": "EQ",
                                "value": email.lower(),
                            }
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company"],
                "limit": 1,
            },
        )
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        return self._request(
            "POST",
            "/crm/v3/objects/contacts",
            json={"properties": properties},
        )

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        return self._request(
            "PATCH",
            f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )

    # ------------------------------------------------------------------ #
    # Notes / Timeline                                                     #
    # ------------------------------------------------------------------ #

    def create_note_on_contact(self, contact_id: str, body: str) -> dict:
        """Create a CRM note and associate it to a contact."""
        timestamp_ms = str(int(time.time() * 1000))
        return self._request(
            "POST",
            "/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": timestamp_ms,
                },
                # Association type 202 = Note → Contact (HubSpot-defined)
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
            },
        )
