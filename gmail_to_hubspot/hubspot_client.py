import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)
_BASE = "https://api.hubapi.com"

# HubSpot association type: Note → Contact (HUBSPOT_DEFINED id 202)
_NOTE_TO_CONTACT_TYPE = {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        self._s = requests.Session()
        self._s.headers.update(
            {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        resp = self._s.post(
            f"{_BASE}/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": ["email", "firstname", "lastname", "company"],
            },
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> str:
        resp = self._s.post(
            f"{_BASE}/crm/v3/objects/contacts", json={"properties": props}
        )
        resp.raise_for_status()
        return resp.json()["id"]

    def update_contact(self, contact_id: str, props: dict) -> None:
        resp = self._s.patch(
            f"{_BASE}/crm/v3/objects/contacts/{contact_id}", json={"properties": props}
        )
        resp.raise_for_status()

    def create_note(self, contact_id: str, body: str) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        resp = self._s.post(
            f"{_BASE}/crm/v3/objects/notes",
            json={
                "properties": {"hs_note_body": body, "hs_timestamp": ts},
                "associations": [
                    {"to": {"id": contact_id}, "types": [_NOTE_TO_CONTACT_TYPE]}
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()["id"]
