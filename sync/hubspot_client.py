"""HubSpot CRM client — contacts + engagement notes via REST API."""
import logging
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"


class HubSpotError(Exception):
    """Raised on non-recoverable HubSpot API errors."""


class HubSpotClient:
    def __init__(self, api_key: str):
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[Dict]:
        """Return the contact dict if found, else None."""
        url = f"{_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company",
                            "hs_lead_status", "leadsource"],
            "limit": 1,
        }
        resp = self._post(url, payload)
        results = resp.get("results", [])
        return results[0] if results else None

    def create_contact(self, props: Dict[str, str]) -> Dict:
        url = f"{_BASE}/crm/v3/objects/contacts"
        return self._post(url, {"properties": props})

    def update_contact(self, contact_id: str, props: Dict[str, str]) -> Dict:
        url = f"{_BASE}/crm/v3/objects/contacts/{contact_id}"
        resp = self._session.patch(url, json={"properties": props})
        self._raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline / notes  (Engagements v1 — supports contactIds directly)
    # ------------------------------------------------------------------

    def create_note(self, contact_id: str, body: str) -> Dict:
        url = f"{_BASE}/engagements/v1/engagements"
        payload = {
            "engagement": {
                "active": True,
                "type": "NOTE",
                "timestamp": int(time.time() * 1000),
            },
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {"body": body},
        }
        resp = self._session.post(url, json=payload)
        self._raise_for_status(resp)
        return resp.json()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, url: str, payload: Dict) -> Dict:
        resp = self._session.post(url, json=payload)
        self._raise_for_status(resp)
        return resp.json()

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        if not resp.ok:
            try:
                detail = resp.json()
            except Exception:
                detail = resp.text
            raise HubSpotError(
                f"HubSpot API error {resp.status_code}: {detail}"
            )
