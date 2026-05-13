"""HubSpot CRM v3 API client for contact management."""

import logging
import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"

CONTACT_PROPERTIES = ["email", "firstname", "lastname", "company", "lead_source"]


class HubSpotClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
        )

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return existing contact dict or None."""
        url = f"{BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}
            ],
            "properties": CONTACT_PROPERTIES,
            "limit": 1,
        }
        resp = self.session.post(url, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts"
        resp = self.session.post(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = self.session.patch(url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def log_email_activity(self, contact_id: str, subject: str, from_header: str) -> dict | None:
        """Create an inbound email engagement on the contact's timeline."""
        url = f"{BASE_URL}/crm/v3/objects/emails"
        payload = {
            "properties": {
                "hs_email_direction": "INCOMING_EMAIL",
                "hs_email_subject": subject,
                "hs_email_status": "RECEIVED",
                "hs_email_text": f"Email received from: {from_header}",
            },
            # association type 198 = email-to-contact (HubSpot defined)
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}
                    ],
                }
            ],
        }
        resp = self.session.post(url, json=payload)
        if not resp.ok:
            log.warning("Timeline event failed (non-critical): %s %s", resp.status_code, resp.text)
            return None
        return resp.json()
