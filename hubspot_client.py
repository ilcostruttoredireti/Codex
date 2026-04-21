from typing import Optional
import requests


class HubSpotClient:
    _BASE = "https://api.hubapi.com"

    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        resp = requests.post(f"{self._BASE}{path}", json=payload, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        resp = requests.patch(f"{self._BASE}{path}", json=payload, headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    def find_by_email(self, email: str) -> Optional[dict]:
        """Return the first contact matching *email*, or None."""
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

    def create_contact(self, properties: dict) -> dict:
        return self._post("/crm/v3/objects/contacts", {"properties": properties})

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        return self._patch(
            f"/crm/v3/objects/contacts/{contact_id}", {"properties": properties}
        )

    def log_email_activity(
        self, contact_id: str, sender_email: str, subject: str
    ) -> bool:
        """Attach an inbound-email engagement to the contact timeline."""
        try:
            # associationTypeId 198 = email → contact (HUBSPOT_DEFINED)
            self._post(
                "/crm/v3/objects/emails",
                {
                    "properties": {
                        "hs_email_direction": "INCOMING_EMAIL",
                        "hs_email_status": "RECEIVED",
                        "hs_email_subject": subject or "(no subject)",
                        "hs_email_text": f"Inbound email from {sender_email}",
                    },
                    "associations": [
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 198,
                                }
                            ],
                        }
                    ],
                },
            )
            return True
        except Exception:
            return False
