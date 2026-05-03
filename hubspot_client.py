import time
import logging
import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# Association type ID: Note → Contact (HubSpot-defined)
_NOTE_TO_CONTACT_ASSOC = 202


class HubSpotClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise ValueError("HUBSPOT_API_KEY non può essere vuoto.")
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs) -> dict | None:
        resp = self.session.get(f"{_BASE}{path}", **kwargs)
        if resp.ok:
            return resp.json()
        logger.error("GET %s → %s %s", path, resp.status_code, resp.text[:200])
        return None

    def _post(self, path: str, payload: dict) -> dict | None:
        resp = self.session.post(f"{_BASE}{path}", json=payload)
        if resp.ok:
            return resp.json()
        logger.error("POST %s → %s %s", path, resp.status_code, resp.text[:200])
        return None

    def _patch(self, path: str, payload: dict) -> dict | None:
        resp = self.session.patch(f"{_BASE}{path}", json=payload)
        if resp.ok:
            return resp.json()
        logger.error("PATCH %s → %s %s", path, resp.status_code, resp.text[:200])
        return None

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Search HubSpot for a contact with the given email. Returns the record or None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source", "website"],
            "limit": 1,
        }
        result = self._post("/crm/v3/objects/contacts/search", payload)
        if result:
            hits = result.get("results", [])
            return hits[0] if hits else None
        return None

    def create_contact(self, properties: dict) -> dict | None:
        """Create a new contact. Returns the created record or None on error."""
        return self._post("/crm/v3/objects/contacts", {"properties": properties})

    def update_contact(self, contact_id: str | int, properties: dict) -> dict | None:
        """Update an existing contact's properties. Returns the updated record or None."""
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties})

    # ------------------------------------------------------------------
    # Timeline / Notes
    # ------------------------------------------------------------------

    def create_note(self, contact_id: str | int, body: str) -> dict | None:
        """Create a note associated with *contact_id*."""
        timestamp_ms = str(int(time.time() * 1000))
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": timestamp_ms,
            },
            "associations": [
                {
                    "to": {"id": str(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": _NOTE_TO_CONTACT_ASSOC,
                        }
                    ],
                }
            ],
        }
        return self._post("/crm/v3/objects/notes", payload)
