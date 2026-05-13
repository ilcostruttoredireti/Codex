import logging
from typing import Optional

import requests

from models import SenderInfo

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACT_SOURCE = "Gmail"
_CONTACT_TAG = "Inbound Gmail"


class HubSpotClient:
    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def find_contact(self, email: str) -> Optional[dict]:
        url = f"{_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname", "company", "leadsource", "hs_tag_ids"],
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        if resp.status_code == 200:
            results = resp.json().get("results", [])
            return results[0] if results else None
        logger.error("HubSpot search failed %s: %s", resp.status_code, resp.text)
        return None

    def create_contact(self, sender: SenderInfo) -> Optional[dict]:
        url = f"{_BASE}/crm/v3/objects/contacts"
        props = self._build_properties(sender)
        resp = requests.post(url, json={"properties": props}, headers=self._headers, timeout=15)
        if resp.status_code == 201:
            contact = resp.json()
            logger.info("Created contact %s (id=%s)", sender.email, contact["id"])
            return contact
        logger.error("HubSpot create failed %s: %s", resp.status_code, resp.text)
        return None

    def update_contact(self, contact_id: str, sender: SenderInfo, existing: dict) -> bool:
        existing_props = existing.get("properties", {})
        patch = {}

        if sender.first_name and not existing_props.get("firstname"):
            patch["firstname"] = sender.first_name
        if sender.last_name and not existing_props.get("lastname"):
            patch["lastname"] = sender.last_name
        if sender.company and not existing_props.get("company"):
            patch["company"] = sender.company

        if not patch:
            return True

        url = f"{_BASE}/crm/v3/objects/contacts/{contact_id}"
        resp = requests.patch(url, json={"properties": patch}, headers=self._headers, timeout=15)
        ok = resp.status_code == 200
        if ok:
            logger.info("Updated contact id=%s fields=%s", contact_id, list(patch))
        else:
            logger.error("HubSpot update failed %s: %s", resp.status_code, resp.text)
        return ok

    def log_email_activity(self, contact_id: str, subject: str, sender_email: str) -> bool:
        url = f"{_BASE}/engagements/v1/engagements"
        payload = {
            "engagement": {"active": True, "type": "EMAIL"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {
                "from": {"email": sender_email},
                "subject": subject or "(no subject)",
                "direction": "INBOUND",
            },
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        if resp.status_code not in (200, 204):
            logger.warning("Could not log activity for contact %s: %s", contact_id, resp.text)
            return False
        return True

    def add_note(self, contact_id: str, body: str) -> bool:
        url = f"{_BASE}/engagements/v1/engagements"
        payload = {
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {"body": body},
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        return resp.status_code in (200, 204)

    @staticmethod
    def _build_properties(sender: SenderInfo) -> dict:
        props: dict = {
            "email": sender.email,
            "leadsource": _CONTACT_SOURCE,
        }
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company
        return props
