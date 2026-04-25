import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"

# Free-provider domains: company name is not inferred from them
PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com", "fastmail.com",
    "yandex.com", "mail.com", "zoho.com", "gmx.com", "tutanota.com",
}


class HubSpotClient:
    def __init__(self, api_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        url = f"{BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "leadsource"],
            "limit": 1,
        }
        resp = self._post(url, payload)
        results = resp.get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts"
        return self._post(url, {"properties": properties})

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = self._session.patch(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Activity: note associated with a contact
    # ------------------------------------------------------------------

    def add_note(self, contact_id: str, body: str) -> Optional[str]:
        timestamp_ms = str(int(time.time() * 1000))
        note_url = f"{BASE_URL}/crm/v3/objects/notes"
        try:
            note = self._post(
                note_url,
                {"properties": {"hs_note_body": body, "hs_timestamp": timestamp_ms}},
            )
            note_id = note["id"]
            assoc_url = (
                f"{BASE_URL}/crm/v3/objects/notes/{note_id}"
                f"/associations/contacts/{contact_id}/202"
            )
            self._session.put(assoc_url).raise_for_status()
            return note_id
        except Exception as exc:
            logger.warning("Note creation failed for contact %s: %s", contact_id, exc)
            return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, url: str, payload: dict) -> dict:
        resp = self._session.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()
