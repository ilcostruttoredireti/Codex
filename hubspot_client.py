"""HubSpot CRM API wrapper — contact CRUD and timeline events."""

import logging
import requests

logger = logging.getLogger(__name__)

CONTACTS_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
EMAILS_URL = "https://api.hubapi.com/crm/v3/objects/emails"


class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return the contact dict if *email* already exists, else None."""
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
            "properties": [
                "email",
                "firstname",
                "lastname",
                "company",
                "leadsource",
            ],
            "limit": 1,
        }
        resp = requests.post(
            f"{CONTACTS_URL}/search", json=payload, headers=self._headers, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("total", 0) > 0:
            return data["results"][0]
        return None

    # ------------------------------------------------------------------
    # Contact write
    # ------------------------------------------------------------------

    def create_contact(self, properties: dict) -> dict:
        """Create a new contact and return the full object dict."""
        resp = requests.post(
            CONTACTS_URL,
            json={"properties": properties},
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str | int, properties: dict) -> dict:
        """Patch *properties* on an existing contact."""
        resp = requests.patch(
            f"{CONTACTS_URL}/{contact_id}",
            json={"properties": properties},
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------

    def create_email_activity(
        self, contact_id: str | int, subject: str, received_at: str
    ) -> dict | None:
        """
        Log the inbound email as a HubSpot Email activity associated with the contact.

        *received_at* should be an RFC-2822 date string from the Gmail header;
        we fall back to the current UTC timestamp if parsing fails.
        """
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone

        try:
            ts = int(parsedate_to_datetime(received_at).timestamp() * 1000)
        except Exception:
            ts = int(datetime.now(timezone.utc).timestamp() * 1000)

        payload = {
            "properties": {
                "hs_email_direction": "INCOMING_EMAIL",
                "hs_email_subject": subject,
                "hs_email_status": "RECEIVED",
                "hs_timestamp": str(ts),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            # 198 = email → contact (standard HubSpot association type)
                            "associationTypeId": 198,
                        }
                    ],
                }
            ],
        }
        try:
            resp = requests.post(
                EMAILS_URL, json=payload, headers=self._headers, timeout=15
            )
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            logger.warning("Timeline event skipped: %s", exc)
            return None
