"""
HubSpot CRM client using the v3 REST API.
Handles contacts and activity notes.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

from config import Config

logger = logging.getLogger(__name__)

_CONTACT_SEARCH = "/crm/v3/objects/contacts/search"
_CONTACT_CREATE = "/crm/v3/objects/contacts"
_CONTACT_UPDATE = "/crm/v3/objects/contacts/{id}"
_NOTE_CREATE = "/crm/v3/objects/notes"
_ASSOC_CREATE = "/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}/note_to_contact"


@dataclass
class HubSpotContact:
    id: str
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    hs_lead_status: str = ""


class HubSpotClient:
    def __init__(self, config: Config):
        self._base = config.HUBSPOT_BASE_URL
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {config.HUBSPOT_API_KEY}",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> HubSpotContact | None:
        payload = {
            "filterGroups": [{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email.lower(),
                }]
            }],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
            "limit": 1,
        }
        resp = self._post(_CONTACT_SEARCH, payload)
        results = resp.get("results", [])
        if not results:
            return None
        return self._parse_contact(results[0])

    def create_contact(self, props: dict[str, str]) -> HubSpotContact:
        payload = {"properties": props}
        resp = self._post(_CONTACT_CREATE, payload)
        return self._parse_contact(resp)

    def update_contact(self, contact_id: str, props: dict[str, str]) -> HubSpotContact:
        url = _CONTACT_UPDATE.format(id=contact_id)
        payload = {"properties": props}
        resp = self._patch(url, payload)
        return self._parse_contact(resp)

    # ------------------------------------------------------------------
    # Activity note
    # ------------------------------------------------------------------

    def log_email_activity(self, contact_id: str, subject: str, snippet: str, received_at: str) -> str:
        """Creates a NOTE in HubSpot and associates it with the contact."""
        body = f"📧 Email ricevuta via Gmail\nOggetto: {subject}\n\n{snippet}"
        note_props = {
            "hs_note_body": body,
            "hs_timestamp": received_at,  # ISO-8601
        }
        resp = self._post(_NOTE_CREATE, {"properties": note_props})
        note_id = resp.get("id", "")

        if note_id:
            self._associate_note(note_id, contact_id)

        return note_id

    def _associate_note(self, note_id: str, contact_id: str) -> None:
        url = _ASSOC_CREATE.format(note_id=note_id, contact_id=contact_id)
        try:
            self._put(url, {})
        except Exception as exc:
            logger.warning("Could not associate note %s with contact %s: %s", note_id, contact_id, exc)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        url = self._base + path
        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        url = self._base + path
        resp = self._session.patch(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _put(self, path: str, payload: dict) -> dict:
        url = self._base + path
        resp = self._session.put(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json() if resp.text else {}

    # ------------------------------------------------------------------

    @staticmethod
    def _parse_contact(raw: dict) -> HubSpotContact:
        props = raw.get("properties", {})
        return HubSpotContact(
            id=raw.get("id", ""),
            email=props.get("email", ""),
            first_name=props.get("firstname", ""),
            last_name=props.get("lastname", ""),
            company=props.get("company", ""),
            hs_lead_status=props.get("hs_lead_status", ""),
        )
