"""
Contact sync logic: maps Gmail sender data → HubSpot properties,
deduplicates by email, creates or updates contacts, and logs activity.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from .gmail_client import SenderInfo
from .hubspot_client import ContactResult, HubSpotClient
from .config import Config

logger = logging.getLogger(__name__)

SyncStatus = Literal["created", "updated", "skipped"]


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: str
    subject: str


class ContactSyncer:
    def __init__(self, hs: HubSpotClient, config: Config) -> None:
        self._hs = hs
        self._config = config

    # ------------------------------------------------------------------
    # Field mapping
    # ------------------------------------------------------------------

    def _build_properties(self, sender: SenderInfo, existing: dict | None = None) -> dict[str, str]:
        """
        Build a HubSpot properties dict from a SenderInfo.

        When updating, only non-empty fields that are currently blank in HubSpot
        are included — so we never overwrite data the user set manually.
        """
        candidates: dict[str, str] = {}

        if sender.first_name:
            candidates["firstname"] = sender.first_name
        if sender.last_name:
            candidates["lastname"] = sender.last_name
        if sender.company:
            candidates["company"] = sender.company

        # Source / tag fields — always set/overwrite with our values
        candidates["lead_source_detail"] = self._config.contact_source

        if existing is None:
            # New contact: set all fields plus email
            candidates["email"] = sender.email
            candidates["hs_lead_status"] = "NEW"
            return candidates

        # Existing contact: only fill in genuinely blank fields
        existing_props: dict[str, str] = existing.get("properties", {})
        update: dict[str, str] = {}

        for key, value in candidates.items():
            current = existing_props.get(key, "") or ""
            if not current.strip():
                update[key] = value

        # Always stamp source
        update["lead_source_detail"] = self._config.contact_source
        return update

    def _tag_label(self) -> str:
        return self._config.inbound_tag

    # ------------------------------------------------------------------
    # Core sync
    # ------------------------------------------------------------------

    def sync(self, sender: SenderInfo) -> SyncResult:
        """
        Sync a single sender to HubSpot.

        Returns a SyncResult with status: created | updated | skipped.
        'skipped' is returned only when the contact already exists and there
        are no fields to update.
        """
        existing = self._hs.find_contact_by_email(sender.email)

        if existing is None:
            # --- Create new contact ---
            props = self._build_properties(sender, existing=None)
            try:
                created = self._hs.create_contact(props)
                contact_id = created["id"]
                logger.info("CREATED  %s  (id=%s)", sender.email, contact_id)

                self._hs.log_email_activity(
                    contact_id, sender.subject, sender.email, sender.date
                )
                return SyncResult(
                    status="created",
                    email=sender.email,
                    hubspot_id=contact_id,
                    subject=sender.subject,
                )
            except Exception as exc:
                logger.error("Failed to create contact %s: %s", sender.email, exc)
                raise

        # --- Update existing contact ---
        contact_id: str = existing["id"]
        update_props = self._build_properties(sender, existing=existing)

        if not update_props or update_props == {"lead_source_detail": self._config.contact_source}:
            # Nothing meaningful to update
            logger.info("SKIPPED  %s  (id=%s, no new data)", sender.email, contact_id)
            self._hs.log_email_activity(
                contact_id, sender.subject, sender.email, sender.date
            )
            return SyncResult(
                status="skipped",
                email=sender.email,
                hubspot_id=contact_id,
                subject=sender.subject,
            )

        try:
            self._hs.update_contact(contact_id, update_props)
            logger.info(
                "UPDATED  %s  (id=%s, fields=%s)",
                sender.email, contact_id, list(update_props.keys()),
            )
            self._hs.log_email_activity(
                contact_id, sender.subject, sender.email, sender.date
            )
            return SyncResult(
                status="updated",
                email=sender.email,
                hubspot_id=contact_id,
                subject=sender.subject,
            )
        except Exception as exc:
            logger.error("Failed to update contact %s: %s", sender.email, exc)
            raise
