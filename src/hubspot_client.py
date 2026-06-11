import logging
import time
from typing import Optional, Tuple

import requests

from .models import ContactResult

logger = logging.getLogger(__name__)


class HubSpotClient:
    BASE_URL = "https://api.hubapi.com"

    def __init__(self, api_token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        })

    def _raise_for_status(self, response: requests.Response) -> None:
        if not response.ok:
            logger.debug("HubSpot %s %s: %s", response.status_code, response.url, response.text)
            response.raise_for_status()

    def search_contact_by_email(self, email: str) -> Optional[dict]:
        response = self.session.post(
            f"{self.BASE_URL}/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [{
                    "filters": [{
                        "propertyName": "email",
                        "operator": "EQ",
                        "value": email,
                    }]
                }],
                "properties": ["email", "firstname", "lastname", "company", "leadsource"],
                "limit": 1,
            },
        )
        self._raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, properties: dict) -> dict:
        response = self.session.post(
            f"{self.BASE_URL}/crm/v3/objects/contacts",
            json={"properties": properties},
        )
        self._raise_for_status(response)
        return response.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        response = self.session.patch(
            f"{self.BASE_URL}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )
        self._raise_for_status(response)
        return response.json()

    def add_note_to_contact(self, contact_id: str, note_body: str) -> Optional[str]:
        """Create an activity note and associate it with the contact."""
        timestamp_ms = str(int(time.time() * 1000))

        resp = self.session.post(
            f"{self.BASE_URL}/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": timestamp_ms,
                }
            },
        )
        if not resp.ok:
            logger.warning("Impossibile creare nota: %s", resp.text)
            return None

        note_id = resp.json()["id"]

        # Associate note → contact (HubSpot defined association typeId 202)
        assoc_resp = self.session.put(
            f"{self.BASE_URL}/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}",
            json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        )
        if not assoc_resp.ok:
            logger.warning(
                "Impossibile associare nota %s al contatto %s: %s",
                note_id, contact_id, assoc_resp.text,
            )

        return note_id

    @staticmethod
    def parse_name(display_name: str) -> Tuple[str, str]:
        parts = display_name.strip().split(" ", 1)
        if len(parts) == 1:
            return parts[0], ""
        return parts[0], parts[1]

    @staticmethod
    def company_from_domain(domain: str, personal_domains: set) -> Optional[str]:
        if domain in personal_domains:
            return None
        name = domain.split(".")[0]
        return name[0].upper() + name[1:] if name else None
