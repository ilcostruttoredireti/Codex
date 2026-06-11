"""
HubSpot client – create, search and update contacts.
Uses the official hubspot-api-client SDK.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicObjectInput

import config

logger = logging.getLogger(__name__)


class SyncStatus(Enum):
    CREATED = auto()
    UPDATED = auto()
    IGNORED = auto()


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    hubspot_id: str
    detail: str = ""

    def __str__(self) -> str:
        label = self.status.name
        return f"[{label}] {self.contact_email}  (HubSpot ID: {self.hubspot_id})"


class HubSpotClient:
    def __init__(self) -> None:
        self._client = hubspot.Client.create(access_token=config.HUBSPOT_ACCESS_TOKEN)

    # ── public API ────────────────────────────────────────────────────────────

    def upsert_contact(
        self,
        email: str,
        first_name: str = "",
        last_name: str = "",
        company: str = "",
    ) -> SyncResult:
        """
        Find an existing contact by email; update missing fields if found,
        create a new contact otherwise.
        """
        existing = self._find_by_email(email)

        if existing:
            contact_id = existing["id"]
            updates = self._build_updates(existing["properties"], first_name, last_name, company)
            if updates:
                self._update_contact(contact_id, updates)
                return SyncResult(SyncStatus.UPDATED, email, contact_id, str(updates))
            return SyncResult(SyncStatus.IGNORED, email, contact_id, "no new fields")

        contact_id = self._create_contact(email, first_name, last_name, company)
        return SyncResult(SyncStatus.CREATED, email, contact_id)

    # ── internal helpers ──────────────────────────────────────────────────────

    def _find_by_email(self, email: str) -> Optional[dict]:
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email,
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if resp.total > 0:
                result = resp.results[0]
                return {"id": result.id, "properties": result.properties}
        except ApiException as exc:
            logger.error("HubSpot search error: %s", exc)
        return None

    def _build_updates(
        self,
        existing: dict,
        first_name: str,
        last_name: str,
        company: str,
    ) -> dict:
        """Only update fields that are currently blank in HubSpot."""
        updates: dict = {}
        if first_name and not existing.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not existing.get("lastname"):
            updates["lastname"] = last_name
        if company and not existing.get("company"):
            updates["company"] = company
        return updates

    def _create_contact(
        self,
        email: str,
        first_name: str,
        last_name: str,
        company: str,
    ) -> str:
        props: dict = {
            "email": email,
            "hs_lead_status": "NEW",
            "lead_source": config.HUBSPOT_CONTACT_SOURCE,
            "hs_analytics_source": "OTHER",
        }
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as exc:
            # 409 = contact already exists (race condition) – retry as update
            if exc.status == 409:
                logger.warning("Race condition on create for %s; searching again.", email)
                existing = self._find_by_email(email)
                if existing:
                    return existing["id"]
            raise

    def _update_contact(self, contact_id: str, properties: dict) -> None:
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=properties),
            )
        except ApiException as exc:
            logger.error("HubSpot update error for %s: %s", contact_id, exc)
            raise
