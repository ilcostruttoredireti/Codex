"""HubSpot CRM client using the v3 REST API directly via requests."""

import time
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# Association type IDs (HubSpot defined, stable)
_NOTE_TO_CONTACT_TYPE_ID = 202


def _make_session(access_token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    })
    retry = Retry(
        total=4,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST", "PATCH"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session


class HubSpotClient:
    def __init__(self, access_token: str):
        self._token = access_token
        self._session = _make_session(access_token)

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return {id, properties} for the contact matching *email*, or None."""
        url = f"{_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "leadsource"],
            "limit": 1,
        }
        resp = self._session.post(url, json=payload)
        self._raise_for_status(resp)
        results = resp.json().get("results", [])
        if results:
            return {"id": results[0]["id"], "properties": results[0]["properties"]}
        return None

    # ------------------------------------------------------------------
    # Contact write operations
    # ------------------------------------------------------------------

    def create_contact(self, props: dict) -> dict | None:
        """
        Create a new contact.  On a 409 CONTACT_EXISTS conflict, return the
        existing contact instead.
        """
        url = f"{_BASE}/crm/v3/objects/contacts"
        resp = self._session.post(url, json={"properties": props})

        if resp.status_code == 409:
            logger.debug("Contact already exists (409) — fetching existing record")
            return self.find_contact_by_email(props.get("email", ""))

        self._raise_for_status(resp)
        data = resp.json()
        return {"id": data["id"], "properties": data["properties"]}

    def update_contact(self, contact_id: str, props: dict) -> dict | None:
        """Update fields on an existing contact."""
        url = f"{_BASE}/crm/v3/objects/contacts/{contact_id}"
        resp = self._session.patch(url, json={"properties": props})
        self._raise_for_status(resp)
        data = resp.json()
        return {"id": data["id"], "properties": data["properties"]}

    # ------------------------------------------------------------------
    # Timeline / notes
    # ------------------------------------------------------------------

    def add_note(self, contact_id: str, body: str) -> None:
        """
        Create a CRM Note and associate it with *contact_id*.
        Failures are logged as warnings so they never block the sync loop.
        """
        url = f"{_BASE}/crm/v3/objects/notes"
        payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
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
        try:
            resp = self._session.post(url, json=payload)
            resp.raise_for_status()
            logger.debug(f"Note created for contact {contact_id}: {resp.json().get('id')}")
        except requests.HTTPError as exc:
            logger.warning(f"Could not add note to contact {contact_id}: {exc}")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            logger.error(
                f"HubSpot API error {resp.status_code}: {resp.text[:400]}"
            )
            raise
