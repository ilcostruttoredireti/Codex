"""HubSpot CRM client — search, create, update contacts and log timeline notes."""

import time
import requests

_BASE = "https://api.hubapi.com"

# Standard HubSpot association type: Note → Contact (HUBSPOT_DEFINED id 202)
_NOTE_TO_CONTACT_ASSOC = {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}


class HubSpotError(Exception):
    pass


class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(f"{_BASE}{path}", json=payload, headers=self._headers, timeout=15)
        if not resp.ok:
            raise HubSpotError(f"POST {path} → {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        resp = requests.patch(f"{_BASE}{path}", json=payload, headers=self._headers, timeout=15)
        if not resp.ok:
            raise HubSpotError(f"PATCH {path} → {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #

    def find_contact(self, email: str) -> dict | None:
        """Return the first HubSpot contact matching *email*, or None."""
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
                "properties": ["email", "firstname", "lastname", "company"],
                "limit": 1,
            },
        )
        results = data.get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        """Create a new contact and return the full response."""
        return self._post("/crm/v3/objects/contacts", {"properties": props})

    def update_contact(self, contact_id: str | int, props: dict) -> dict:
        """Patch an existing contact."""
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})

    # ------------------------------------------------------------------ #
    # Timeline (note engagement)
    # ------------------------------------------------------------------ #

    def add_email_note(self, contact_id: str | int, sender_email: str, gmail_msg_id: str) -> bool:
        """
        Attach a note to the contact timeline recording the inbound Gmail email.
        Returns True on success, False if the API call fails (non-fatal).
        """
        try:
            self._post(
                "/crm/v3/objects/notes",
                {
                    "properties": {
                        "hs_note_body": (
                            f"📥 Email inbound ricevuta da {sender_email}\n"
                            f"Fonte: Gmail | Tag: Inbound Gmail\n"
                            f"Gmail Message-ID: {gmail_msg_id}"
                        ),
                        "hs_timestamp": str(int(time.time() * 1000)),
                    },
                    "associations": [
                        {
                            "to": {"id": contact_id},
                            "types": [_NOTE_TO_CONTACT_ASSOC],
                        }
                    ],
                },
            )
            return True
        except HubSpotError:
            return False
