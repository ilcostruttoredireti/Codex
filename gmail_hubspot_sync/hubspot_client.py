"""HubSpot CRM client — contacts and notes via REST API."""

import datetime
from typing import Optional

import requests


class HubSpotClient:
    _BASE = "https://api.hubapi.com"

    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the first contact matching the email, or None."""
        resp = self._session.post(
            f"{self._BASE}/crm/v3/objects/contacts/search",
            json={
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
                "properties": [
                    "email",
                    "firstname",
                    "lastname",
                    "company",
                    "hs_lead_source",
                ],
                "limit": 1,
            },
        )

        if resp.status_code == 200:
            results = resp.json().get("results", [])
            return results[0] if results else None

        print(f"[HubSpot] search error {resp.status_code}: {resp.text[:300]}")
        return None

    def create_contact(self, properties: dict) -> Optional[dict]:
        """Create a new contact. Returns the created record or None on failure."""
        resp = self._session.post(
            f"{self._BASE}/crm/v3/objects/contacts",
            json={"properties": properties},
        )

        if resp.status_code == 201:
            return resp.json()

        # 409 Conflict = contact already exists (race condition safety)
        if resp.status_code == 409:
            existing_id = (
                resp.json()
                .get("message", "")
                .split("Existing ID: ")[-1]
                .strip()
            )
            return {"id": existing_id, "properties": properties} if existing_id else None

        print(f"[HubSpot] create error {resp.status_code}: {resp.text[:300]}")
        return None

    def update_contact(self, contact_id: str, properties: dict) -> bool:
        """Patch an existing contact with the given properties."""
        resp = self._session.patch(
            f"{self._BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )
        if resp.status_code != 200:
            print(
                f"[HubSpot] update {contact_id} error "
                f"{resp.status_code}: {resp.text[:300]}"
            )
        return resp.status_code == 200

    # ------------------------------------------------------------------
    # Notes / Engagements
    # ------------------------------------------------------------------

    def add_note_to_contact(self, contact_id: str, note_body: str) -> Optional[str]:
        """Create a note object and associate it with a contact. Returns note ID."""
        ts_ms = int(datetime.datetime.utcnow().timestamp() * 1000)

        resp = self._session.post(
            f"{self._BASE}/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": str(ts_ms),
                }
            },
        )

        if resp.status_code != 201:
            print(f"[HubSpot] note create error {resp.status_code}: {resp.text[:300]}")
            return None

        note_id = resp.json()["id"]

        # Associate the note with the contact (HubSpot-defined type 202)
        assoc = self._session.put(
            f"{self._BASE}/crm/v4/objects/notes/{note_id}"
            f"/associations/contacts/{contact_id}",
            json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        )
        if assoc.status_code not in (200, 201):
            print(
                f"[HubSpot] note association error "
                f"{assoc.status_code}: {assoc.text[:200]}"
            )

        return note_id
