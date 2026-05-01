"""HubSpot CRM client for contact upsert and activity logging."""

from __future__ import annotations

import logging
import time
from typing import Literal

import requests

API_BASE = "https://api.hubapi.com"

# HubSpot association type ID: note → contact
_NOTE_TO_CONTACT_ASSOC_TYPE = 202

logger = logging.getLogger(__name__)

UpsertStatus = Literal["created", "updated", "ignored"]


class HubSpotClient:
    def __init__(self, api_token: str):
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_contact(self, contact: dict) -> tuple[UpsertStatus, str]:
        """
        Create or update a HubSpot contact keyed by email address.

        Returns (status, contact_id) where status is one of:
          "created"  – new contact was created
          "updated"  – existing contact was updated with missing fields
          "ignored"  – existing contact already had all fields; nothing changed
        """
        email = contact["email"]
        existing = self._search_by_email(email)

        properties = self._build_properties(contact)

        if existing:
            contact_id: str = existing["id"]
            existing_props: dict = existing.get("properties", {})
            update_props = {
                k: v
                for k, v in properties.items()
                if k != "email" and v and not existing_props.get(k)
            }
            if update_props:
                self._update_contact(contact_id, update_props)
                return "updated", contact_id
            return "ignored", contact_id

        result = self._create_contact(properties)
        return "created", result["id"]

    def add_email_activity_note(self, contact_id: str, subject: str, sender_email: str) -> None:
        """Attach a timeline note recording the inbound Gmail email."""
        body = (
            f"📧 Email inbound ricevuta via Gmail\n"
            f"Mittente: {sender_email}\n"
            f"Oggetto: {subject or '(nessun oggetto)'}\n"
            f"Tag: Inbound Gmail"
        )
        url = f"{API_BASE}/crm/v3/objects/notes"
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
                            "associationTypeId": _NOTE_TO_CONTACT_ASSOC_TYPE,
                        }
                    ],
                }
            ],
        }
        try:
            self._post(url, payload)
        except requests.HTTPError as exc:
            logger.warning("Could not create activity note for contact %s: %s", contact_id, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_by_email(self, email: str) -> dict | None:
        url = f"{API_BASE}/crm/v3/objects/contacts/search"
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": [
                "email", "firstname", "lastname", "company",
                "leadsource", "hs_analytics_source",
            ],
            "limit": 1,
        }
        data = self._post(url, payload)
        results = data.get("results", [])
        return results[0] if results else None

    def _create_contact(self, properties: dict) -> dict:
        url = f"{API_BASE}/crm/v3/objects/contacts"
        return self._post(url, {"properties": properties})

    def _update_contact(self, contact_id: str, properties: dict) -> dict:
        url = f"{API_BASE}/crm/v3/objects/contacts/{contact_id}"
        return self._patch(url, {"properties": properties})

    @staticmethod
    def _build_properties(contact: dict) -> dict:
        props: dict[str, str] = {"email": contact["email"]}

        if contact.get("first_name"):
            props["firstname"] = contact["first_name"]
        if contact.get("last_name"):
            props["lastname"] = contact["last_name"]
        if contact.get("company"):
            props["company"] = contact["company"]

        # Built-in HubSpot source field (accepts free-form string via API)
        props["leadsource"] = "Gmail"

        return props

    def _post(self, url: str, payload: dict) -> dict:
        resp = requests.post(url, headers=self._headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, url: str, payload: dict) -> dict:
        resp = requests.patch(url, headers=self._headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()
