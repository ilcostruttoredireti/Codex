"""
HubSpot CRM client: search, create, and update contacts via the v3 REST API.
Also creates Note engagements associated with contacts for activity tracking.
"""

import logging
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACTS = f"{_BASE}/crm/v3/objects/contacts"
_NOTES = f"{_BASE}/crm/v3/objects/notes"


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the first HubSpot contact matching the given email, or None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status", "lead_source"],
            "limit": 1,
        }
        resp = self._session.post(f"{_CONTACTS}/search", json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Contact creation / update
    # ------------------------------------------------------------------

    def create_contact(self, properties: dict) -> dict:
        """Create a new HubSpot contact and return the created object."""
        resp = self._session.post(_CONTACTS, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: dict) -> dict:
        """Patch an existing HubSpot contact (only non-empty fields)."""
        resp = self._session.patch(f"{_CONTACTS}/{contact_id}", json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Activity / Note creation
    # ------------------------------------------------------------------

    def create_email_received_note(
        self,
        contact_id: str,
        sender_email: str,
        subject: str,
        date: str,
    ) -> Optional[str]:
        """
        Create a Note engagement recording an inbound Gmail email,
        then associate it with the given contact.
        Returns the note ID or None on failure.
        """
        body = (
            f"Inbound Gmail email received\n"
            f"From: {sender_email}\n"
            f"Subject: {subject or '(no subject)'}\n"
            f"Date: {date or 'unknown'}"
        )
        note_payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": _iso_now(),
            }
        }

        try:
            resp = self._session.post(_NOTES, json=note_payload)
            resp.raise_for_status()
            note_id = resp.json()["id"]

            # Associate note → contact
            assoc_url = (
                f"{_NOTES}/{note_id}/associations/contacts/{contact_id}/note_to_contact"
            )
            assoc_resp = self._session.put(assoc_url)
            assoc_resp.raise_for_status()

            return note_id
        except requests.HTTPError as e:
            logger.warning("Could not create note for contact %s: %s", contact_id, e)
            return None

    # ------------------------------------------------------------------
    # Property helpers
    # ------------------------------------------------------------------

    def build_contact_properties(self, sender: dict, source_label: str = "Gmail") -> dict:
        """
        Build the HubSpot properties dict from a sender info dict.
        Only includes non-empty values so we don't overwrite existing data with blanks.
        """
        props: dict = {"email": sender["email"]}

        if sender.get("first_name"):
            props["firstname"] = sender["first_name"]
        if sender.get("last_name"):
            props["lastname"] = sender["last_name"]
        if sender.get("company"):
            props["company"] = sender["company"]

        props["lead_source"] = source_label
        # hs_lead_status is read-only; use a custom free-text field for the tag
        props["hs_analytics_source_data_1"] = "Inbound Gmail"

        return props

    def merge_properties(self, existing: dict, new_props: dict) -> dict:
        """
        Return only the properties from new_props that are missing or empty
        in the existing HubSpot contact — so we never overwrite richer data.
        """
        current = existing.get("properties", {})
        updates = {}
        for key, value in new_props.items():
            if key == "email":
                continue  # never patch email
            existing_value = current.get(key, "")
            if not existing_value and value:
                updates[key] = value
        return updates


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
