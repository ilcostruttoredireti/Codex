import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# Association type: Note → Contact (HubSpot defined)
_NOTE_TO_CONTACT_ASSOC_TYPE = 202


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    def _get(self, path: str, **kwargs) -> dict:
        resp = self._session.get(f"{_BASE}{path}", **kwargs)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(f"{_BASE}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        resp = self._session.patch(f"{_BASE}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
            "limit": 1,
        }
        data = self._post("/crm/v3/objects/contacts/search", payload)
        return data["results"][0] if data.get("total", 0) > 0 else None

    def create_contact(self, props: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str, props: dict) -> dict:
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    # ------------------------------------------------------------------
    # Notes / timeline
    # ------------------------------------------------------------------

    def create_note_for_contact(self, contact_id: str, body: str) -> None:
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": str(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": _NOTE_TO_CONTACT_ASSOC_TYPE,
                        }
                    ],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/notes", payload)
        except requests.HTTPError as exc:
            logger.warning(f"Could not create note for contact {contact_id}: {exc}")
