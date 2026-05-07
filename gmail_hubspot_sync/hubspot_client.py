"""HubSpot API client — contacts and timeline activities."""
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config import Config, COMMON_EMAIL_DOMAINS
from gmail_client import SenderInfo

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"


@dataclass
class ContactResult:
    status: str          # "created" | "updated" | "ignored"
    email: str
    contact_id: Optional[str]


class HubSpotClient:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._headers = {
            "Authorization": f"Bearer {config.hubspot_api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs) -> requests.Response:
        return requests.get(f"{_BASE}{path}", headers=self._headers, **kwargs)

    def _post(self, path: str, payload: dict) -> requests.Response:
        return requests.post(f"{_BASE}{path}", headers=self._headers, json=payload)

    def _patch(self, path: str, payload: dict) -> requests.Response:
        return requests.patch(f"{_BASE}{path}", headers=self._headers, json=payload)

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact object or None."""
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
        resp = self._post("/crm/v3/objects/contacts/search", payload)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: dict) -> str:
        """Create a new contact and return its ID."""
        resp = self._post("/crm/v3/objects/contacts", {"properties": props})
        resp.raise_for_status()
        return resp.json()["id"]

    def update_contact(self, contact_id: str, props: dict) -> None:
        """Patch only the provided properties on an existing contact."""
        resp = self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
        resp.raise_for_status()

    # ------------------------------------------------------------------
    # Timeline / engagement
    # ------------------------------------------------------------------

    def add_email_engagement(self, contact_id: str, sender: SenderInfo) -> None:
        """Record an inbound email as a HubSpot note engagement."""
        payload = {
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {
                "body": (
                    f"<b>Inbound Gmail</b><br>"
                    f"<b>From:</b> {sender.full_name} &lt;{sender.email}&gt;<br>"
                    f"<b>Subject:</b> {sender.subject}<br>"
                    f"<b>Message ID:</b> {sender.message_id}"
                )
            },
        }
        resp = self._post("/engagements/v1/engagements", payload)
        if not resp.ok:
            # Non-fatal — log and continue
            logger.warning(
                "Could not create engagement for contact %s: %s %s",
                contact_id, resp.status_code, resp.text,
            )

    # ------------------------------------------------------------------
    # High-level sync
    # ------------------------------------------------------------------

    @staticmethod
    def _company_from_domain(domain: str) -> str:
        """Best-effort company name from email domain."""
        if domain in COMMON_EMAIL_DOMAINS:
            return ""
        # Strip TLD(s): "acme.co.uk" → "acme"
        parts = domain.split(".")
        name = parts[0] if parts else ""
        return name.replace("-", " ").title()

    @staticmethod
    def _merge_props(existing: dict, candidate: dict) -> dict:
        """Return only the candidate fields that are missing in the existing contact."""
        current = existing.get("properties", {})
        return {
            k: v
            for k, v in candidate.items()
            if v and not current.get(k)
        }

    def sync_sender(self, sender: SenderInfo, add_engagement: bool = True) -> ContactResult:
        """
        Main entry point: upsert a contact from a Gmail sender.
        Returns a ContactResult with status, email, and HubSpot contact ID.
        """
        # Build canonical properties
        company = self._company_from_domain(sender.domain)
        props: dict = {
            "email": sender.email,
            "hs_lead_status": "NEW",
        }
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if company:
            props["company"] = company
        # Custom source property (requires matching HubSpot field name)
        props["lead_source_detail"] = self.config.hubspot_contact_source

        existing = self.find_contact_by_email(sender.email)

        if existing:
            contact_id = existing["id"]
            delta = self._merge_props(existing, props)
            if delta:
                self.update_contact(contact_id, delta)
                status = "updated"
                logger.info("Updated contact %s (%s)", contact_id, sender.email)
            else:
                status = "ignored"
                logger.info("No changes for contact %s (%s)", contact_id, sender.email)
        else:
            contact_id = self.create_contact(props)
            status = "created"
            logger.info("Created contact %s (%s)", contact_id, sender.email)

        if add_engagement and status != "ignored":
            self.add_email_engagement(contact_id, sender)

        return ContactResult(status=status, email=sender.email, contact_id=contact_id)
