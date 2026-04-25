"""
HubSpot client: search, create, and update contacts via the HubSpot v3 CRM API.

Uses a private-app access token (Bearer auth) — no OAuth dance needed.

Endpoints used:
  POST /crm/v3/objects/contacts/search  → find by email
  POST /crm/v3/objects/contacts         → create contact
  PATCH /crm/v3/objects/contacts/{id}   → update contact
  POST /crm/v3/objects/contacts/{id}/associations/... (optional engagements)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_TIMEOUT = 15  # seconds


@dataclass
class ContactResult:
    hubspot_id: str
    email: str
    status: str   # "created" | "updated" | "skipped"


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        if not access_token:
            raise ValueError("HUBSPOT_ACCESS_TOKEN is not set.")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._session.post(f"{_BASE}{path}", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        resp = self._session.patch(f"{_BASE}{path}", json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return the first matching HubSpot contact record or None."""
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
                "hs_lead_status", "lead_source_detail",
            ],
            "limit": 1,
        }
        try:
            data = self._post("/crm/v3/objects/contacts/search", payload)
        except requests.HTTPError as exc:
            logger.error("HubSpot search failed for %s: %s", email, exc)
            return None

        results = data.get("results", [])
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, properties: dict[str, str]) -> dict:
        payload = {"properties": properties}
        return self._post("/crm/v3/objects/contacts", payload)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, properties: dict[str, str]) -> dict:
        payload = {"properties": properties}
        return self._patch(f"/crm/v3/objects/contacts/{contact_id}", payload)

    # ------------------------------------------------------------------
    # Timeline engagement (email received activity)
    # ------------------------------------------------------------------

    def log_email_activity(
        self,
        contact_id: str,
        subject: str,
        sender_email: str,
        date: str,
    ) -> None:
        """Create a 'note' engagement recording the inbound email."""
        note_body = (
            f"Inbound email received via Gmail sync.\n"
            f"From: {sender_email}\n"
            f"Subject: {subject}\n"
            f"Date: {date}"
        )
        payload = {
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": _parse_timestamp(date),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,   # Note → Contact
                        }
                    ],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/notes", payload)
            logger.debug("Activity note created for contact %s", contact_id)
        except requests.HTTPError as exc:
            # Non-fatal: log and continue
            logger.warning("Could not create activity note for %s: %s", contact_id, exc)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _parse_timestamp(date_str: str) -> int:
    """Convert RFC 2822 email date to epoch milliseconds. Falls back to now."""
    import time
    import email.utils
    try:
        parsed = email.utils.parsedate_to_datetime(date_str)
        return int(parsed.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)
