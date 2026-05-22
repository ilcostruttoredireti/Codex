"""HubSpot CRM v3 wrapper – contacts, notes, associations."""

from datetime import datetime, timezone
import requests

BASE_URL = "https://api.hubapi.com"

# HubSpot association type ID: note → contact
_NOTE_TO_CONTACT_TYPE_ID = 202


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
        """Return the first matching contact or None."""
        url = f"{BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
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
            "properties": ["email", "firstname", "lastname", "company", "lead_source"],
            "limit": 1,
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts"
        resp = requests.post(
            url, json={"properties": properties}, headers=self._headers, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = requests.patch(
            url, json={"properties": properties}, headers=self._headers, timeout=15
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline / notes
    # ------------------------------------------------------------------

    def add_inbound_email_note(self, contact_id: str, sender_email: str, subject: str) -> dict:
        """Create a note associated with the contact recording the inbound email."""
        url = f"{BASE_URL}/crm/v3/objects/notes"
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        body = f"Email in entrata da {sender_email}\nOggetto: {subject or '(nessun oggetto)'}"
        payload = {
            "properties": {
                "hs_timestamp": str(ts_ms),
                "hs_note_body": body,
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
                        }
                    ],
                }
            ],
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
