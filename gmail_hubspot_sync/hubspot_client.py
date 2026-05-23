from __future__ import annotations
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACT_PROPS = ["email", "firstname", "lastname", "company", "leadsource"]
# associationTypeId 202 = note → contact (HubSpot-defined)
_NOTE_TO_CONTACT_ASSOC_TYPE = 202


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------ contacts

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        resp = self._session.post(
            f"{_BASE}/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
                ],
                "properties": _CONTACT_PROPS,
                "limit": 1,
            },
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return None
        r = results[0]
        return {"id": r["id"], "properties": r.get("properties", {})}

    def create_contact(self, properties: dict) -> dict:
        resp = self._session.post(
            f"{_BASE}/crm/v3/objects/contacts", json={"properties": properties}
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data["id"], "properties": data.get("properties", {})}

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        resp = self._session.patch(
            f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data["id"], "properties": data.get("properties", {})}

    # ------------------------------------------------------------------ notes

    def create_note(self, contact_id: str, body: str) -> Optional[str]:
        timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        resp = self._session.post(
            f"{_BASE}/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": str(timestamp_ms),
                },
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": _NOTE_TO_CONTACT_ASSOC_TYPE,
                            }
                        ],
                    }
                ],
            },
        )
        if resp.status_code in (200, 201):
            return resp.json().get("id")
        logger.warning("Note creation failed (%s): %s", resp.status_code, resp.text[:200])
        return None
