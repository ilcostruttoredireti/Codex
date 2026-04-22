import logging
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"


class HubSpotClient:
    def __init__(self, api_key: str, inbound_list_id: Optional[str] = None):
        self._inbound_list_id = inbound_list_id
        self._http = httpx.Client(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    # ------------------------------------------------------------------
    # Contact search / create / update
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        url = f"{_BASE}/crm/v3/objects/contacts/search"
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
        resp = self._http.post(url, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        resp = self._http.post(
            f"{_BASE}/crm/v3/objects/contacts", json={"properties": props}
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, props: dict) -> dict:
        resp = self._http.patch(
            f"{_BASE}/crm/v3/objects/contacts/{contact_id}", json={"properties": props}
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline activity (note)
    # ------------------------------------------------------------------

    def add_note(self, contact_id: str, body: str) -> Optional[str]:
        """Create a note on the contact's timeline."""
        ts_ms = str(int(time.time() * 1000))
        try:
            resp = self._http.post(
                f"{_BASE}/crm/v3/objects/notes",
                json={"properties": {"hs_note_body": body, "hs_timestamp": ts_ms}},
            )
            resp.raise_for_status()
            note_id: str = resp.json()["id"]

            # Associate note → contact
            self._http.put(
                f"{_BASE}/crm/v3/objects/notes/{note_id}"
                f"/associations/contacts/{contact_id}/note_to_contact"
            )
            return note_id
        except Exception as exc:
            logger.warning(f"Could not create note for contact {contact_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Optional: add contact to a static list (tag = "Inbound Gmail")
    # ------------------------------------------------------------------

    def add_to_inbound_list(self, vid: str):
        """
        Add a contact to the configured static list via the legacy v1 API.
        Requires HUBSPOT_INBOUND_LIST_ID to be set.
        """
        if not self._inbound_list_id:
            return
        try:
            resp = self._http.post(
                f"{_BASE}/contacts/v1/lists/{self._inbound_list_id}/add",
                json={"vids": [int(vid)]},
            )
            if resp.status_code not in (200, 204):
                logger.warning(
                    f"List add returned {resp.status_code}: {resp.text[:200]}"
                )
        except Exception as exc:
            logger.warning(f"Could not add contact {vid} to inbound list: {exc}")

    # ------------------------------------------------------------------

    def close(self):
        self._http.close()
