import time

import requests

BASE_URL = "https://api.hubapi.com"


class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Returns the full contact object or None if not found."""
        url = f"{BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "lifecyclestage"],
            "limit": 1,
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data["results"][0] if data.get("total", 0) > 0 else None

    def create_contact(self, properties: dict) -> dict:
        """Creates a new contact and returns the created object."""
        url = f"{BASE_URL}/crm/v3/objects/contacts"
        resp = requests.post(url, json={"properties": properties}, headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        """Updates an existing contact. Only pass fields to change."""
        url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = requests.patch(url, json={"properties": properties}, headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline / Notes
    # ------------------------------------------------------------------

    def add_note_to_contact(self, contact_id: str, body: str) -> str | None:
        """
        Creates a note and associates it with the contact.
        Returns the note ID on success, None on failure (non-blocking).
        """
        try:
            note_url = f"{BASE_URL}/crm/v3/objects/notes"
            note_payload = {
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": str(int(time.time() * 1000)),
                }
            }
            resp = requests.post(note_url, json=note_payload, headers=self._headers, timeout=15)
            resp.raise_for_status()
            note_id = resp.json()["id"]

            # Associate note → contact (associationTypeId 202 = note_to_contact)
            assoc_url = (
                f"{BASE_URL}/crm/v4/objects/notes/{note_id}"
                f"/associations/contacts/{contact_id}"
            )
            assoc_payload = [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}]
            requests.put(assoc_url, json=assoc_payload, headers=self._headers, timeout=15)

            return note_id
        except Exception:
            return None
