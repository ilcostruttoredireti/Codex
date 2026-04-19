"""HubSpot CRM client – contacts CRUD + timeline activity via REST API v3."""

import logging
import time
from dataclasses import dataclass
from typing import Literal

import requests

from .contact_extractor import ContactInfo

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACTS_URL = f"{_BASE}/crm/v3/objects/contacts"
_ENGAGEMENTS_URL = f"{_BASE}/crm/v3/objects/emails"

SyncStatus = Literal["created", "updated", "ignored"]

INBOUND_GMAIL_TAG = "Inbound Gmail"
CONTACT_SOURCE = "Gmail"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str
    message_subject: str = ""


class HubSpotClient:
    def __init__(self, access_token: str):
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        self._session = requests.Session()
        self._session.headers.update(self._headers)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def sync_contact(
        self,
        contact: ContactInfo,
        message_id: str,
        subject: str,
        snippet: str,
        received_date: str,
    ) -> SyncResult:
        """
        Find or create the HubSpot contact for *contact*, then log a timeline
        email engagement.  Returns a SyncResult describing what happened.
        """
        existing = self._find_contact_by_email(contact.email)

        if existing:
            contact_id = existing["id"]
            self._update_missing_fields(contact_id, existing["properties"], contact)
            status: SyncStatus = "updated"
        else:
            contact_id = self._create_contact(contact)
            status = "created"

        self._create_email_engagement(
            contact_id=contact_id,
            from_email=contact.email,
            subject=subject,
            body_preview=snippet,
            received_date=received_date,
        )

        return SyncResult(
            status=status,
            email=contact.email,
            contact_id=contact_id,
            message_subject=subject,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_contact_by_email(self, email: str) -> dict | None:
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
        resp = self._post(f"{_CONTACTS_URL}/search", payload)
        results = resp.get("results", [])
        return results[0] if results else None

    def _create_contact(self, contact: ContactInfo) -> str:
        properties = _build_properties(contact)
        resp = self._post(_CONTACTS_URL, {"properties": properties})
        contact_id = resp["id"]
        logger.info("Created HubSpot contact %s (id=%s)", contact.email, contact_id)
        return contact_id

    def _update_missing_fields(
        self, contact_id: str, existing_props: dict, contact: ContactInfo
    ) -> None:
        updates: dict = {}

        def _set_if_missing(prop: str, value: str) -> None:
            if value and not existing_props.get(prop):
                updates[prop] = value

        _set_if_missing("firstname", contact.first_name)
        _set_if_missing("lastname", contact.last_name)
        _set_if_missing("company", contact.company)
        _set_if_missing("lead_source_detail", CONTACT_SOURCE)

        # Always ensure the tag property is set
        current_tag = existing_props.get("hs_lead_status", "")
        if INBOUND_GMAIL_TAG not in (current_tag or ""):
            updates["hs_lead_status"] = INBOUND_GMAIL_TAG

        if updates:
            self._patch(f"{_CONTACTS_URL}/{contact_id}", {"properties": updates})
            logger.info("Updated contact %s fields: %s", contact_id, list(updates))

    def _create_email_engagement(
        self,
        contact_id: str,
        from_email: str,
        subject: str,
        body_preview: str,
        received_date: str,
    ) -> None:
        """Log an inbound email engagement on the contact timeline."""
        payload = {
            "properties": {
                "hs_email_direction": "INBOUND",
                "hs_email_status": "RECEIVED",
                "hs_email_subject": subject or "(no subject)",
                "hs_email_text": body_preview or "",
                "hs_timestamp": _iso_to_epoch_ms(received_date),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 198,  # email → contact
                        }
                    ],
                }
            ],
        }
        try:
            self._post(_ENGAGEMENTS_URL, payload)
            logger.debug("Created email engagement for contact %s", contact_id)
        except requests.HTTPError as exc:
            # Non-fatal: log and continue
            logger.warning("Could not create email engagement: %s", exc)

    # ------------------------------------------------------------------
    # HTTP helpers with basic retry on 429
    # ------------------------------------------------------------------

    def _post(self, url: str, payload: dict) -> dict:
        return self._request("POST", url, payload)

    def _patch(self, url: str, payload: dict) -> dict:
        return self._request("PATCH", url, payload)

    def _request(self, method: str, url: str, payload: dict, retries: int = 3) -> dict:
        for attempt in range(retries):
            resp = self._session.request(method, url, json=payload)
            if resp.status_code == 429:
                wait = 2 ** attempt
                logger.warning("HubSpot rate limit hit, waiting %ds", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        resp.raise_for_status()
        return {}


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_properties(contact: ContactInfo) -> dict:
    props: dict = {
        "email": contact.email,
        "hs_lead_status": INBOUND_GMAIL_TAG,
        "lead_source_detail": CONTACT_SOURCE,
    }
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    return props


def _iso_to_epoch_ms(date_str: str) -> str:
    """Convert an RFC 2822 date string to epoch milliseconds string for HubSpot."""
    if not date_str:
        return str(int(time.time() * 1000))
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(date_str)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return str(int(time.time() * 1000))
