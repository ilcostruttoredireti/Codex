"""
HubSpot sync: creates or updates contacts from Gmail sender data,
avoids duplicates, and logs a timeline activity per email received.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicUpsertObject

from gmail_monitor import SenderInfo

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    hubspot_contact_id: Optional[str]
    detail: str = ""


# HubSpot property names
_PROP_EMAIL = "email"
_PROP_FIRST = "firstname"
_PROP_LAST = "lastname"
_PROP_COMPANY = "company"
_PROP_SOURCE = "hs_lead_status"          # reused below via custom prop
_PROP_CONTACT_SOURCE = "lead_source_detail"  # custom; fallback to lifecyclestage note
_TAG_PROPERTY = "hs_tag"                 # doesn't exist natively; we use notes instead


class HubSpotSync:
    """
    Syncs Gmail sender info into HubSpot CRM contacts.

    Deduplication key: email address (case-insensitive).
    Strategy:
      - Search contact by email.
      - If found → fill in any empty fields with new data.
      - If not found → create new contact.
    """

    def __init__(self, access_token: str, owner_id: Optional[str] = None):
        self._client = hubspot.Client.create(access_token=access_token)
        self._owner_id = owner_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync(self, info: SenderInfo) -> SyncResult:
        """Process one SenderInfo and return a SyncResult."""
        existing = self._find_contact(info.email)

        if existing:
            result = self._update_contact(existing, info)
        else:
            result = self._create_contact(info)

        if result.hubspot_contact_id:
            self._log_timeline_activity(result.hubspot_contact_id, info)

        return result

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _find_contact(self, email: str) -> Optional[dict]:
        """Return the existing contact dict or None."""
        try:
            search_request = PublicObjectSearchRequest(
                filter_groups=[
                    FilterGroup(
                        filters=[
                            Filter(
                                property_name=_PROP_EMAIL,
                                operator="EQ",
                                value=email.lower(),
                            )
                        ]
                    )
                ],
                properties=[
                    _PROP_EMAIL,
                    _PROP_FIRST,
                    _PROP_LAST,
                    _PROP_COMPANY,
                    "lifecyclestage",
                    "leadsource",
                ],
                limit=1,
            )
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0]
        except ApiException as exc:
            logger.error("HubSpot search error for %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def _create_contact(self, info: SenderInfo) -> SyncResult:
        props = self._build_properties(info, existing_props={})
        try:
            response = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            contact_id = response.id
            logger.info("Created contact %s (id=%s)", info.email, contact_id)
            return SyncResult(
                status=SyncStatus.CREATED,
                contact_email=info.email,
                hubspot_contact_id=contact_id,
            )
        except ApiException as exc:
            logger.error("HubSpot create error for %s: %s", info.email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=info.email,
                hubspot_contact_id=None,
                detail=str(exc),
            )

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def _update_contact(self, existing, info: SenderInfo) -> SyncResult:
        existing_props = existing.properties or {}
        updates = self._build_properties(info, existing_props=existing_props)

        # Only send fields that are currently empty in HubSpot
        filtered = {
            k: v
            for k, v in updates.items()
            if v and not existing_props.get(k)
        }

        # Always refresh leadsource tag if not already "Gmail"
        if existing_props.get("leadsource") != "Gmail":
            filtered["leadsource"] = "Gmail"

        contact_id = existing.id

        if not filtered:
            logger.debug("No new fields to update for %s — skipping.", info.email)
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=info.email,
                hubspot_contact_id=contact_id,
                detail="All fields already populated",
            )

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.models.SimplePublicObjectInput(
                    properties=filtered
                ),
            )
            logger.info(
                "Updated contact %s (id=%s) fields: %s",
                info.email,
                contact_id,
                list(filtered.keys()),
            )
            return SyncResult(
                status=SyncStatus.UPDATED,
                contact_email=info.email,
                hubspot_contact_id=contact_id,
            )
        except ApiException as exc:
            logger.error("HubSpot update error for %s: %s", info.email, exc)
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=info.email,
                hubspot_contact_id=contact_id,
                detail=str(exc),
            )

    # ------------------------------------------------------------------
    # Property builder
    # ------------------------------------------------------------------

    def _build_properties(self, info: SenderInfo, existing_props: dict) -> dict:
        props: dict[str, str] = {
            _PROP_EMAIL: info.email,
            "leadsource": "Gmail",
        }

        if info.first_name:
            props[_PROP_FIRST] = info.first_name
        if info.last_name:
            props[_PROP_LAST] = info.last_name
        if info.company:
            props[_PROP_COMPANY] = info.company

        if self._owner_id:
            props["hubspot_owner_id"] = self._owner_id

        return props

    # ------------------------------------------------------------------
    # Timeline activity (engagement note)
    # ------------------------------------------------------------------

    def _log_timeline_activity(self, contact_id: str, info: SenderInfo) -> None:
        """
        Creates an Engagement (NOTE) on the contact recording the inbound email.
        HubSpot's basic CRM API does not expose a timeline event endpoint directly,
        so we use the Engagements v1 API via a raw call.
        """
        try:
            body = {
                "engagement": {
                    "active": True,
                    "type": "NOTE",
                },
                "associations": {
                    "contactIds": [int(contact_id)],
                },
                "metadata": {
                    "body": (
                        f"📧 Inbound Gmail\n"
                        f"Da: {info.full_name} <{info.email}>\n"
                        f"Oggetto: {info.subject}\n"
                        f"Data: {info.received_at}\n"
                        f"Tag: Inbound Gmail"
                    ),
                },
            }
            api_response = self._client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body=body,
                auth_settings=["oauth2"],
                response_type="object",
                _return_http_data_only=True,
            )
            logger.debug(
                "Timeline activity logged for contact %s", contact_id
            )
        except Exception as exc:
            # Non-critical — log but don't fail the sync
            logger.warning(
                "Could not log timeline activity for %s: %s", contact_id, exc
            )
