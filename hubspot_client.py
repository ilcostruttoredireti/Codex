"""HubSpot CRM API client – contacts management."""

import time
from typing import Any

import requests


class HubSpotClient:
    BASE_URL = "https://api.hubapi.com"

    def __init__(self, token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )

    # ── Search ────────────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return the full contact object if found, else None."""
        payload = {
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
            "properties": [
                "email", "firstname", "lastname", "company",
                "hs_lead_status", "lead_source",
            ],
            "limit": 1,
        }
        resp = self._post("/crm/v3/objects/contacts/search", payload)
        results = resp.get("results", [])
        return results[0] if results else None

    # ── Create / Update ───────────────────────────────────────────────────────

    def create_contact(self, properties: dict[str, str]) -> dict:
        """Create a new contact and return the created object."""
        return self._post("/crm/v3/objects/contacts", {"properties": properties})

    def update_contact(self, contact_id: str, properties: dict[str, str]) -> dict:
        """Patch an existing contact with non-empty properties."""
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties})

    # ── Timeline event (optional) ─────────────────────────────────────────────

    def create_timeline_event(
        self,
        event_template_id: str,
        contact_id: str,
        tokens: dict[str, str],
    ) -> dict:
        """Create a timeline event associated with a contact."""
        payload = {
            "eventTemplateId": event_template_id,
            "objectId": contact_id,
            "tokens": tokens,
            "timestamp": int(time.time() * 1000),
        }
        return self._post(
            f"/crm/v3/timeline/events",
            payload,
        )

    # ── Notes (used as fallback timeline activity) ─────────────────────────────

    def create_note(self, contact_id: str, body: str) -> dict:
        """Create an engagement note associated with a contact."""
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
        }
        note = self._post("/crm/v3/objects/notes", payload)

        # Associate note with the contact
        assoc_url = (
            f"/crm/v4/objects/notes/{note['id']}/associations/contacts/{contact_id}"
        )
        self._put(
            assoc_url,
            [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        )
        return note

    # ── Low-level HTTP ────────────────────────────────────────────────────────

    def _post(self, path: str, payload: Any) -> dict:
        resp = self._session.post(f"{self.BASE_URL}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: Any) -> dict:
        resp = self._session.patch(f"{self.BASE_URL}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, payload: Any) -> dict:
        resp = self._session.put(f"{self.BASE_URL}{path}", json=payload)
        resp.raise_for_status()
        # 204 No Content for associations
        return resp.json() if resp.content else {}
