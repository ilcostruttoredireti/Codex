import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACT_PROPS = ["email", "firstname", "lastname", "company", "lead_source"]


def _ms_now() -> int:
    return int(time.time() * 1000)


def _make_session(api_key: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    )
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


class HubSpotClient:
    def __init__(self, api_key: str):
        self._session = _make_session(api_key)

    # ------------------------------------------------------------------
    # Contacts
    # ------------------------------------------------------------------

    def find_by_email(self, email: str) -> Optional[dict]:
        url = f"{_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": _CONTACT_PROPS,
            "limit": 1,
        }
        resp = self._session.post(url, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> dict:
        resp = self._session.post(
            f"{_BASE}/crm/v3/objects/contacts", json={"properties": props}
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, props: dict) -> dict:
        resp = self._session.patch(
            f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": props},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Notes / timeline
    # ------------------------------------------------------------------

    def create_email_note(
        self, contact_id: str, sender_email: str, subject: str
    ) -> None:
        body = f"📧 Email inbound ricevuta\nMittente: {sender_email}\nOggetto: {subject}"
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(_ms_now()),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,
                        }
                    ],
                }
            ],
        }
        resp = self._session.post(f"{_BASE}/crm/v3/objects/notes", json=payload)
        if not resp.ok:
            logger.warning(
                "Note creation failed for contact %s: %s", contact_id, resp.text
            )
