import os
import logging
from typing import Optional
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.hubapi.com"

_COMMON_PROVIDERS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "protonmail.com", "libero.it",
    "virgilio.it", "tiscali.it", "fastwebnet.it",
}


class HubSpotClient:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ["HUBSPOT_API_KEY"]
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # -------------------------------------------------------------------------
    # Contact lookup
    # -------------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        url = f"{BASE_URL}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [{"filters": [{"propertyName": "email", "operator": "EQ", "value": email}]}],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
            "limit": 1,
        }
        resp = requests.post(url, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    # -------------------------------------------------------------------------
    # Contact write
    # -------------------------------------------------------------------------

    def create_contact(self, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts"
        resp = requests.post(url, json={"properties": properties}, headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = requests.patch(url, json={"properties": properties}, headers=self._headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------------------------------
    # Timeline engagement (note)
    # -------------------------------------------------------------------------

    def add_email_note(self, contact_id: str, subject: str, email_date: str, sender_email: str):
        """Create a 'NOTE' engagement linked to the contact to record the received email."""
        url = f"{BASE_URL}/crm/v3/objects/notes"
        timestamp = _iso_to_ms(email_date)
        body = {
            "properties": {
                "hs_timestamp": str(timestamp),
                "hs_note_body": (
                    f"Email ricevuta tramite Gmail\n"
                    f"Da: {sender_email}\n"
                    f"Oggetto: {subject or '(nessun oggetto)'}\n"
                    f"Tag: Inbound Gmail"
                ),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
                }
            ],
        }
        resp = requests.post(url, json=body, headers=self._headers, timeout=15)
        if not resp.ok:
            logger.warning("Impossibile creare nota HubSpot per contatto %s: %s", contact_id, resp.text)

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    @staticmethod
    def company_from_domain(email: str) -> str:
        """Derive a company name from the email domain, ignoring common providers."""
        if "@" not in email:
            return ""
        domain = email.split("@", 1)[1].lower()
        if domain in _COMMON_PROVIDERS:
            return ""
        # e.g. "acme.com" → "Acme"
        return domain.split(".")[0].capitalize()

    @staticmethod
    def split_name(full_name: str) -> tuple[str, str]:
        parts = full_name.strip().split(None, 1)
        return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _iso_to_ms(date_str: str) -> int:
    """Best-effort: convert an RFC 2822 or ISO date string to Unix ms. Falls back to now."""
    from email.utils import parsedate_to_datetime
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp() * 1000)
    except Exception:
        pass
    return int(datetime.now(timezone.utc).timestamp() * 1000)
