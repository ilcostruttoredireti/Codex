"""
HubSpot CRM API client for contact management.

Uses the HubSpot v3 Contacts API with a Private App access token.
Handles search, create, update, and timeline activity creation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

import requests

HUBSPOT_BASE = "https://api.hubapi.com"
CONTACTS_ENDPOINT = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
TIMELINE_ENDPOINT = f"{HUBSPOT_BASE}/crm/v3/timeline/events"
SEARCH_ENDPOINT = f"{CONTACTS_ENDPOINT}/search"

# HubSpot rate-limit: 10 req/s for Private Apps; we stay conservative
_REQUEST_INTERVAL = 0.15  # seconds between requests


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str
    message: str = ""


class HubSpotClient:
    """
    Thin wrapper around HubSpot CRM v3 for contact upsert operations.

    Args:
        access_token: HubSpot Private App token (HUBSPOT_ACCESS_TOKEN env var).
        timeline_app_id: Optional app ID for creating timeline events.
    """

    def __init__(self, access_token: str, timeline_app_id: str | None = None) -> None:
        self._token = access_token
        self._timeline_app_id = timeline_app_id
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })
        self._last_request_ts = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def upsert_contact(
        self,
        email: str,
        first_name: str,
        last_name: str,
        company: str,
        subject: str = "",
        message_id: str = "",
    ) -> SyncResult:
        """
        Create or update a HubSpot contact keyed on email address.

        Returns a SyncResult with status CREATED, UPDATED, or IGNORED.
        IGNORED is returned when the contact already exists and all
        relevant fields are already populated (no update needed).
        """
        existing = self._find_contact_by_email(email)

        if existing is None:
            contact_id = self._create_contact(email, first_name, last_name, company)
            if self._timeline_app_id:
                self._create_timeline_event(contact_id, email, subject, message_id)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=email,
                contact_id=contact_id,
            )

        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates = self._build_update_payload(props, first_name, last_name, company)

        if not updates:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                contact_id=contact_id,
                message="Nessun campo da aggiornare",
            )

        self._update_contact(contact_id, updates)
        return SyncResult(
            status=SyncStatus.UPDATED,
            email=email,
            contact_id=contact_id,
            message=f"Campi aggiornati: {', '.join(updates.keys())}",
        )

    # ------------------------------------------------------------------
    # Contact CRUD
    # ------------------------------------------------------------------

    def _find_contact_by_email(self, email: str) -> dict[str, Any] | None:
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
        resp = self._post(SEARCH_ENDPOINT, payload)
        results = resp.get("results", [])
        return results[0] if results else None

    def _create_contact(
        self, email: str, first_name: str, last_name: str, company: str
    ) -> str:
        props: dict[str, str] = {
            "email": email,
            "hs_lead_status": "NEW",
            "leadsource": "Gmail",
        }
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        resp = self._post(CONTACTS_ENDPOINT, {"properties": props})
        return resp["id"]

    def _update_contact(self, contact_id: str, properties: dict[str, str]) -> None:
        self._patch(f"{CONTACTS_ENDPOINT}/{contact_id}", {"properties": properties})

    def _build_update_payload(
        self,
        existing_props: dict[str, Any],
        first_name: str,
        last_name: str,
        company: str,
    ) -> dict[str, str]:
        """Only populate fields that are currently empty in HubSpot."""
        updates: dict[str, str] = {}
        if first_name and not existing_props.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not existing_props.get("lastname"):
            updates["lastname"] = last_name
        if company and not existing_props.get("company"):
            updates["company"] = company
        # Always ensure lead source is set
        if not existing_props.get("leadsource"):
            updates["leadsource"] = "Gmail"
        return updates

    # ------------------------------------------------------------------
    # Timeline events
    # ------------------------------------------------------------------

    def _create_timeline_event(
        self,
        contact_id: str,
        email: str,
        subject: str,
        message_id: str,
    ) -> None:
        """Create a CRM timeline entry for the received email."""
        if not self._timeline_app_id:
            return
        payload = {
            "eventTemplateId": "EMAIL_RECEIVED",
            "objectId": contact_id,
            "tokens": {
                "subject": subject or "(nessun oggetto)",
                "source": "Gmail",
                "messageId": message_id,
            },
        }
        try:
            self._post(TIMELINE_ENDPOINT, payload)
        except requests.HTTPError:
            # Timeline creation is best-effort; don't fail the whole sync
            pass

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _post(self, url: str, payload: dict) -> dict[str, Any]:
        self._throttle()
        resp = self._session.post(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, url: str, payload: dict) -> dict[str, Any]:
        self._throttle()
        resp = self._session.patch(url, json=payload)
        resp.raise_for_status()
        return resp.json()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < _REQUEST_INTERVAL:
            time.sleep(_REQUEST_INTERVAL - elapsed)
        self._last_request_ts = time.monotonic()
