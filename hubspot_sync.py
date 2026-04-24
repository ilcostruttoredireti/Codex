"""
HubSpot contact sync: searches for an existing contact by email, then
creates or updates it.  Optionally logs a timeline activity and applies
the "Inbound Gmail" tag via a contact list.
"""

import logging
from dataclasses import dataclass
from enum import Enum

import requests

from contact_extractor import ContactInfo

logger = logging.getLogger(__name__)

HUBSPOT_API_BASE = "https://api.hubapi.com"


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str | None
    detail: str = ""


class HubSpotSync:
    def __init__(self, access_token: str):
        self._token = access_token
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, **kwargs) -> requests.Response:
        return self._session.get(f"{HUBSPOT_API_BASE}{path}", **kwargs)

    def _post(self, path: str, **kwargs) -> requests.Response:
        return self._session.post(f"{HUBSPOT_API_BASE}{path}", **kwargs)

    def _patch(self, path: str, **kwargs) -> requests.Response:
        return self._session.patch(f"{HUBSPOT_API_BASE}{path}", **kwargs)

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def _find_contact_by_email(self, email: str) -> dict | None:
        """Return the existing HubSpot contact dict or None."""
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
        resp = self._post("/crm/v3/objects/contacts/search", json=payload)
        if resp.status_code != 200:
            logger.warning("HubSpot search error %s: %s", resp.status_code, resp.text)
            return None
        results = resp.json().get("results", [])
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Create / update
    # ------------------------------------------------------------------

    def _build_properties(self, contact: ContactInfo, existing: dict | None) -> dict:
        """
        Build the properties dict for a create or update call.
        For updates we only include fields that are currently empty so we
        never overwrite richer data already in HubSpot.
        """
        existing_props: dict = (existing or {}).get("properties", {})

        def keep(field: str, new_value: str) -> bool:
            """True when we should include *field* in the payload."""
            if not new_value:
                return False
            if existing is None:
                return True  # create – include everything we have
            # update – only fill in blanks
            return not existing_props.get(field)

        props: dict = {}

        if keep("email", contact.email):
            props["email"] = contact.email
        if keep("firstname", contact.first_name):
            props["firstname"] = contact.first_name
        if keep("lastname", contact.last_name):
            props["lastname"] = contact.last_name
        if keep("company", contact.company):
            props["company"] = contact.company

        # Always stamp the source on first creation; on updates only if blank
        if keep("lead_source", "Gmail") or existing is None:
            props["lead_source"] = "Gmail"

        return props

    def _create_contact(self, contact: ContactInfo) -> dict | None:
        props = self._build_properties(contact, None)
        resp = self._post("/crm/v3/objects/contacts", json={"properties": props})
        if resp.status_code in (200, 201):
            return resp.json()
        logger.error("HubSpot create failed %s: %s", resp.status_code, resp.text)
        return None

    def _update_contact(self, contact_id: str, contact: ContactInfo, existing: dict) -> dict | None:
        props = self._build_properties(contact, existing)
        if not props:
            return existing  # nothing to update
        resp = self._patch(f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props})
        if resp.status_code == 200:
            return resp.json()
        logger.error("HubSpot update failed %s: %s", resp.status_code, resp.text)
        return None

    # ------------------------------------------------------------------
    # Timeline activity  (CRM Engagement note)
    # ------------------------------------------------------------------

    def _log_email_activity(self, contact_id: str, contact: ContactInfo) -> None:
        """Create an Inbound Email note engagement linked to the contact."""
        payload = {
            "engagement": {"active": True, "type": "NOTE"},
            "associations": {"contactIds": [int(contact_id)]},
            "metadata": {
                "body": (
                    f"Inbound Gmail ricevuta da {contact.raw_sender}.<br>"
                    f"Oggetto: {contact.subject or '(nessun oggetto)'}<br>"
                    f"Tag: Inbound Gmail"
                )
            },
        }
        resp = self._post("/engagements/v1/engagements", json=payload)
        if resp.status_code not in (200, 201):
            logger.warning(
                "Could not log timeline activity for %s: %s %s",
                contact_id,
                resp.status_code,
                resp.text,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self, contact: ContactInfo, log_activity: bool = True) -> SyncResult:
        """
        Upsert a contact in HubSpot.

        Returns a SyncResult with status CREATED, UPDATED, or IGNORED
        (IGNORED when HubSpot calls fail and the contact could not be saved).
        """
        existing = self._find_contact_by_email(contact.email)

        if existing is None:
            created = self._create_contact(contact)
            if created is None:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    contact_id=None,
                    detail="HubSpot create call failed",
                )
            contact_id = created["id"]
            if log_activity:
                self._log_email_activity(contact_id, contact)
            logger.info("Created HubSpot contact %s (%s)", contact_id, contact.email)
            return SyncResult(status=SyncStatus.CREATED, email=contact.email, contact_id=contact_id)

        else:
            contact_id = existing["id"]
            updated = self._update_contact(contact_id, contact, existing)
            if updated is None:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    contact_id=contact_id,
                    detail="HubSpot update call failed",
                )
            if log_activity:
                self._log_email_activity(contact_id, contact)
            logger.info("Updated HubSpot contact %s (%s)", contact_id, contact.email)
            return SyncResult(status=SyncStatus.UPDATED, email=contact.email, contact_id=contact_id)
