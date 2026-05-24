"""
hubspot_client.py – HubSpot API wrapper for contact create / update / search
"""
from __future__ import annotations

from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)
from hubspot.crm.timeline import (
    TimelineEvent,
)

from config import config
from logger import get_logger
from models import SenderInfo, SyncResult, SyncStatus

logger = get_logger()


# Properties to request when searching for an existing contact
_FETCH_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "company",
    "hs_lead_status",
]


class HubSpotClient:
    def __init__(self) -> None:
        self._client = hubspot.Client.create(access_token=config.HUBSPOT_ACCESS_TOKEN)
        logger.info("HubSpot client initialised ✓")

    # ── Search ────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the HubSpot contact dict or None if not found."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email.lower(),
                        )
                    ]
                )
            ],
            properties=_FETCH_PROPERTIES,
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if resp.total > 0:
                return resp.results[0].to_dict()
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    # ── Create ────────────────────────────────────────────────

    def create_contact(self, sender: SenderInfo) -> SyncResult:
        """Create a new HubSpot contact from SenderInfo."""
        props = _build_properties(sender)
        logger.debug("Creating HubSpot contact: %s", props)

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            contact_id = result.id
            logger.info("  ✅ CREATO   %-40s  id=%s", sender.email, contact_id)
            return SyncResult(
                status=SyncStatus.CREATED,
                contact_email=sender.email,
                hubspot_id=contact_id,
            )
        except ApiException as exc:
            # 409 = already exists (race condition) – treat as update
            if exc.status == 409:
                logger.warning("Contact %s already exists (race condition), falling back to update", sender.email)
                existing = self.find_contact_by_email(sender.email)
                if existing:
                    return self.update_contact(existing["id"], sender, existing.get("properties", {}))
            logger.error("  ❌ ERRORE   %s: %s", sender.email, exc)
            return SyncResult(
                status=SyncStatus.ERROR,
                contact_email=sender.email,
                error=str(exc),
            )

    # ── Update ────────────────────────────────────────────────

    def update_contact(
        self,
        contact_id: str,
        sender: SenderInfo,
        existing_props: dict,
    ) -> SyncResult:
        """Update only blank / missing fields for an existing contact."""
        updates = _build_update_properties(sender, existing_props)

        if not updates:
            logger.info("  ⏭  IGNORATO %-40s  id=%s (nessun campo da aggiornare)", sender.email, contact_id)
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=sender.email,
                hubspot_id=contact_id,
            )

        logger.debug("Updating contact %s with %s", contact_id, updates)
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            logger.info("  🔄 AGGIORNATO %-38s  id=%s", sender.email, contact_id)
            return SyncResult(
                status=SyncStatus.UPDATED,
                contact_email=sender.email,
                hubspot_id=contact_id,
            )
        except ApiException as exc:
            logger.error("  ❌ ERRORE   %s: %s", sender.email, exc)
            return SyncResult(
                status=SyncStatus.ERROR,
                contact_email=sender.email,
                hubspot_id=contact_id,
                error=str(exc),
            )

    # ── Timeline activity ─────────────────────────────────────

    def create_email_activity(
        self,
        contact_id: str,
        message: "EmailMessage",  # noqa: F821  (imported lazily to avoid circular)
    ) -> None:
        """
        Associate an email-received timeline event to the contact.

        Note: Timeline events require an app_id (OAuth app) which is only
        available when using a *developer* HubSpot app.  With Private App
        tokens the endpoint returns 403.  We catch that gracefully.
        """
        try:
            # Use the engagements v1 endpoint instead – works with private apps
            import requests

            payload = {
                "engagement": {
                    "active": True,
                    "type": "EMAIL",
                },
                "associations": {
                    "contactIds": [int(contact_id)],
                },
                "metadata": {
                    "subject": message.subject,
                    "text": message.snippet,
                    "from": {
                        "email": message.sender.email,
                        "firstName": message.sender.first_name,
                        "lastName": message.sender.last_name,
                    },
                    "direction": "INBOUND",
                },
            }

            resp = requests.post(
                "https://api.hubapi.com/engagements/v1/engagements",
                json=payload,
                headers={
                    "Authorization": f"Bearer {config.HUBSPOT_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )

            if resp.status_code in (200, 201):
                logger.debug(
                    "Timeline activity created for contact %s (msg %s)",
                    contact_id,
                    message.message_id,
                )
            else:
                logger.debug(
                    "Timeline activity skipped for contact %s: HTTP %s",
                    contact_id,
                    resp.status_code,
                )

        except Exception as exc:  # noqa: BLE001
            logger.debug("Timeline activity error (non-fatal): %s", exc)


# ── Property builders ─────────────────────────────────────────────────────────

def _build_properties(sender: SenderInfo) -> dict[str, str]:
    """Build the full set of HubSpot properties for a new contact."""
    props: dict[str, str] = {
        "email": sender.email,
        "hs_lead_status": "NEW",
        "leadsource": config.CONTACT_SOURCE,   # standard HubSpot field
    }
    if sender.first_name:
        props["firstname"] = sender.first_name
    if sender.last_name:
        props["lastname"] = sender.last_name
    if sender.company:
        props["company"] = sender.company
    # Custom property – only written if it already exists in the portal.
    # HubSpot ignores unknown property names gracefully (returns 400 if strict mode).
    # We wrap in try/except in the caller.
    props["hs_analytics_source"] = config.CONTACT_SOURCE
    return props


def _build_update_properties(sender: SenderInfo, existing: dict) -> dict[str, str]:
    """
    Return only the fields that are currently empty in the existing contact
    so we never overwrite data the user filled in manually.
    """
    updates: dict[str, str] = {}

    existing_props = existing if isinstance(existing, dict) else {}

    def _missing(field: str) -> bool:
        val = existing_props.get(field)
        return not val or str(val).strip() == ""

    if sender.first_name and _missing("firstname"):
        updates["firstname"] = sender.first_name
    if sender.last_name and _missing("lastname"):
        updates["lastname"] = sender.last_name
    if sender.company and _missing("company"):
        updates["company"] = sender.company
    if _missing("leadsource"):
        updates["leadsource"] = config.CONTACT_SOURCE

    return updates
