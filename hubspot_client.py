import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"


class HubSpotClient:
    def __init__(self, config):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.hubspot_access_token}",
                "Content-Type": "application/json",
            }
        )

    # ── internal helpers ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{_BASE}{path}"
        for attempt in range(3):
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                logger.warning("HubSpot rate-limited, retrying in %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()
        return {}

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json=payload)

    def _patch(self, path: str, payload: dict) -> dict:
        return self._request("PATCH", path, json=payload)

    # ── public API ────────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[Dict]:
        data = self._post(
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company", "leadsource"],
                "limit": 1,
            },
        )
        if data.get("total", 0) > 0:
            c = data["results"][0]
            return {"id": c["id"], "properties": c["properties"]}
        return None

    def create_contact(self, properties: Dict) -> Dict:
        data = self._post("/crm/v3/objects/contacts", {"properties": properties})
        return {"id": data["id"], "properties": data.get("properties", {})}

    def update_contact(self, contact_id: str, properties: Dict) -> Dict:
        data = self._patch(
            f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties}
        )
        return {"id": data["id"], "properties": data.get("properties", {})}

    def add_note(self, contact_id: str, body: str) -> None:
        """Creates an engagement note associated with the contact."""
        try:
            self._post(
                "/engagements/v1/engagements",
                {
                    "engagement": {"active": True, "type": "NOTE"},
                    "associations": {"contactIds": [int(contact_id)]},
                    "metadata": {"body": body},
                },
            )
        except Exception as exc:
            logger.warning("Could not add note to contact %s: %s", contact_id, exc)
