import logging
import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.hubapi.com"

# HubSpot association type: note → contact (HUBSPOT_DEFINED id 202)
_NOTE_TO_CONTACT_TYPE = {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}


class HubSpotClient:
    def __init__(self, api_key: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _request(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        url = f"{_BASE_URL}{endpoint}"
        resp = self._session.request(method, url, **kwargs)

        # Automatic retry on rate-limit
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", "10"))
            logger.warning("HubSpot rate limit — waiting %ds", wait)
            time.sleep(wait)
            resp = self._session.request(method, url, **kwargs)

        return resp

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
            "limit": 1,
        }
        resp = self._request("POST", "/crm/v3/objects/contacts/search", json=payload)

        if resp.status_code != 200:
            logger.error(
                "HubSpot search error [%s]: %s", resp.status_code, resp.text[:200]
            )
            return None

        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict | None:
        resp = self._request(
            "POST", "/crm/v3/objects/contacts", json={"properties": properties}
        )

        if resp.status_code not in (200, 201):
            logger.error(
                "HubSpot create contact error [%s]: %s",
                resp.status_code,
                resp.text[:200],
            )
            return None

        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict | None:
        resp = self._request(
            "PATCH",
            f"/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )

        if resp.status_code != 200:
            logger.error(
                "HubSpot update contact error [%s]: %s",
                resp.status_code,
                resp.text[:200],
            )
            return None

        return resp.json()

    # ------------------------------------------------------------------
    # Notes / engagements
    # ------------------------------------------------------------------

    def add_note_to_contact(
        self, contact_id: str, note_body: str, timestamp_ms: int = None
    ) -> dict | None:
        ts = str(timestamp_ms or int(time.time() * 1000))

        payload = {
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": ts,
            },
            "associations": [
                {
                    "to": {"id": str(contact_id)},
                    "types": [_NOTE_TO_CONTACT_TYPE],
                }
            ],
        }

        resp = self._request("POST", "/crm/v3/objects/notes", json=payload)

        if resp.status_code not in (200, 201):
            logger.error(
                "HubSpot add note error [%s]: %s", resp.status_code, resp.text[:200]
            )
            return None

        return resp.json()
