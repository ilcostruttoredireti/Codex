"""HubSpot API client — search, create, and update contacts."""

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

CONTACTS_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
SEARCH_URL = f"{CONTACTS_URL}/search"
TIMELINE_URL = "https://api.hubapi.com/crm/v3/timeline/events"


@dataclass
class ContactData:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    source: str = "Gmail"
    tags: list[str] = field(default_factory=lambda: ["Inbound Gmail"])


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored"
    email: str
    contact_id: str
    reason: str = ""


class HubSpotClient:
    def __init__(self, access_token: str, app_id: str = ""):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._app_id = app_id

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, **params) -> requests.Response:
        return requests.get(url, headers=self._headers, params=params, timeout=15)

    def _post(self, url: str, payload: dict) -> requests.Response:
        return requests.post(url, headers=self._headers, json=payload, timeout=15)

    def _patch(self, url: str, payload: dict) -> requests.Response:
        return requests.patch(url, headers=self._headers, json=payload, timeout=15)

    def _search_by_email(self, email: str) -> dict | None:
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status"],
            "limit": 1,
        }
        resp = self._post(SEARCH_URL, payload)
        if resp.status_code != 200:
            logger.error("HubSpot search failed: %s %s", resp.status_code, resp.text)
            resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def _build_properties(self, contact: ContactData, existing: dict | None = None) -> dict:
        """
        Build the properties dict for create/update.
        On update, only include fields that add missing information.
        """
        existing_props = existing.get("properties", {}) if existing else {}

        def missing(key: str) -> bool:
            return not existing_props.get(key)

        props: dict[str, Any] = {}

        if contact.first_name and missing("firstname"):
            props["firstname"] = contact.first_name
        if contact.last_name and missing("lastname"):
            props["lastname"] = contact.last_name
        if contact.company and missing("company"):
            props["company"] = contact.company

        # Always set source on new contacts; never overwrite on existing
        if existing is None:
            props["email"] = contact.email
            props["hs_lead_status"] = "NEW"
            props["leadsource"] = contact.source

        return props

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_contact(self, contact: ContactData) -> SyncResult:
        """Search for the contact by email, then create or update."""
        existing = self._search_by_email(contact.email)

        if existing:
            contact_id = existing["id"]
            props = self._build_properties(contact, existing)

            if not props:
                logger.info("No new fields for %s — skipping update", contact.email)
                return SyncResult(
                    status="ignored",
                    email=contact.email,
                    contact_id=contact_id,
                    reason="no new fields to update",
                )

            resp = self._patch(f"{CONTACTS_URL}/{contact_id}", {"properties": props})
            if resp.status_code not in (200, 204):
                logger.error("HubSpot update failed: %s %s", resp.status_code, resp.text)
                resp.raise_for_status()

            logger.info("Updated contact %s (id=%s)", contact.email, contact_id)
            return SyncResult(status="updated", email=contact.email, contact_id=contact_id)

        else:
            props = self._build_properties(contact)
            resp = self._post(CONTACTS_URL, {"properties": props})
            if resp.status_code == 409:
                # Race condition: contact created between search and insert
                existing_after = self._search_by_email(contact.email)
                contact_id = existing_after["id"] if existing_after else "unknown"
                return SyncResult(
                    status="ignored",
                    email=contact.email,
                    contact_id=contact_id,
                    reason="conflict on create (already exists)",
                )
            if resp.status_code not in (200, 201):
                logger.error("HubSpot create failed: %s %s", resp.status_code, resp.text)
                resp.raise_for_status()

            contact_id = resp.json()["id"]
            logger.info("Created contact %s (id=%s)", contact.email, contact_id)
            return SyncResult(status="created", email=contact.email, contact_id=contact_id)

    def add_timeline_event(
        self,
        contact_id: str,
        email_subject: str,
        email_snippet: str,
        message_id: str,
    ) -> None:
        """Record the received email as a HubSpot timeline activity."""
        if not self._app_id:
            # Timeline events require a HubSpot app; skip if not configured
            return

        payload = {
            "eventTemplateId": self._app_id,
            "objectId": contact_id,
            "tokens": {
                "subject": email_subject,
                "snippet": email_snippet,
                "messageId": message_id,
            },
        }
        resp = self._post(TIMELINE_URL, payload)
        if resp.status_code not in (200, 201, 204):
            # Non-fatal — log and continue
            logger.warning(
                "Timeline event failed for contact %s: %s %s",
                contact_id,
                resp.status_code,
                resp.text,
            )
