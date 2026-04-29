"""HubSpot CRM API client for contact management."""

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

HUBSPOT_API_BASE = "https://api.hubapi.com"
CONTACTS_SEARCH = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts/search"
CONTACTS_BASE = f"{HUBSPOT_API_BASE}/crm/v3/objects/contacts"
TIMELINE_EVENTS = f"{HUBSPOT_API_BASE}/crm/v3/timeline/events"

# HubSpot internal name for the "Gmail" contact source tag.
INBOUND_GMAIL_TAG = "Inbound Gmail"


@dataclass
class ContactData:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    source: str = "Gmail"
    tags: list[str] = field(default_factory=lambda: [INBOUND_GMAIL_TAG])


class HubSpotClient:
    def __init__(self, api_key: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return the existing HubSpot contact record or None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_status", "contact_source"],
            "limit": 1,
        }
        resp = self.session.post(CONTACTS_SEARCH, json=payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, data: ContactData) -> str:
        """Create a new contact. Returns the HubSpot contact ID."""
        props = self._build_properties(data)
        resp = self.session.post(CONTACTS_BASE, json={"properties": props})
        if resp.status_code == 409:
            # Race condition: contact already exists.
            existing = self.find_contact_by_email(data.email)
            if existing:
                return existing["id"]
        resp.raise_for_status()
        contact_id = resp.json()["id"]
        logger.info("Created contact %s (id=%s)", data.email, contact_id)
        return contact_id

    def update_contact(self, contact_id: str, current_props: dict, data: ContactData) -> None:
        """Patch only the fields that are currently blank in HubSpot."""
        updates = self._missing_fields(current_props, data)
        if not updates:
            logger.debug("No fields to update for contact %s", contact_id)
            return
        resp = self.session.patch(f"{CONTACTS_BASE}/{contact_id}", json={"properties": updates})
        resp.raise_for_status()
        logger.info("Updated contact %s (id=%s) with %s", data.email, contact_id, list(updates.keys()))

    def log_email_activity(self, contact_id: str, app_id: int, event_template_id: str, subject: str) -> None:
        """
        Create a timeline event on the contact record.
        Requires a Timeline Event Template ID configured in your HubSpot app.
        """
        payload = {
            "eventTemplateId": event_template_id,
            "objectId": contact_id,
            "tokens": {"subject": subject},
        }
        resp = self.session.post(
            f"{HUBSPOT_API_BASE}/crm/v3/timeline/{app_id}/events",
            json=payload,
        )
        if resp.ok:
            logger.debug("Timeline event logged for contact %s", contact_id)
        else:
            logger.warning("Could not log timeline event: %s", resp.text)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_properties(self, data: ContactData) -> dict[str, Any]:
        props: dict[str, Any] = {"email": data.email}
        if data.first_name:
            props["firstname"] = data.first_name
        if data.last_name:
            props["lastname"] = data.last_name
        if data.company:
            props["company"] = data.company
        if data.source:
            props["hs_analytics_source"] = "OTHER_CAMPAIGNS"
            props["contact_source"] = data.source
        return props

    def _missing_fields(self, current_props: dict, data: ContactData) -> dict[str, Any]:
        """Return properties from data that are absent/empty in current_props."""
        updates: dict[str, Any] = {}
        mapping = {
            "firstname": data.first_name,
            "lastname": data.last_name,
            "company": data.company,
        }
        for prop, value in mapping.items():
            if value and not current_props.get(prop):
                updates[prop] = value
        if data.source and not current_props.get("contact_source"):
            updates["contact_source"] = data.source
        return updates
