"""Core sync logic: process a SenderInfo and upsert it in HubSpot."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .models import SenderInfo
from .hubspot_client import HubSpotClient
from .config import SKIP_DOMAINS

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    reason: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.email}"]
        if self.contact_id:
            parts.append(f"HubSpot ID: {self.contact_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


class ContactSyncService:
    def __init__(self, hubspot_client: Optional[HubSpotClient] = None):
        self._hs = hubspot_client or HubSpotClient()
        # In-process cache to avoid redundant API calls within the same run
        self._seen_emails: set[str] = set()

    def process(self, sender: SenderInfo, log_activity: bool = True) -> SyncResult:
        email = sender.email.lower().strip()

        # --- skip rules ---
        if sender.domain.lower() in SKIP_DOMAINS:
            return SyncResult(SyncStatus.SKIPPED, email, None, "domain in skip list")

        if email in self._seen_emails:
            return SyncResult(SyncStatus.SKIPPED, email, None, "already processed this run")
        self._seen_emails.add(email)

        # --- upsert ---
        existing = self._hs.find_contact_by_email(email)

        if existing is None:
            contact_id = self._hs.create_contact(
                email=email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                company=sender.company,
            )
            if contact_id is None:
                return SyncResult(SyncStatus.SKIPPED, email, None, "HubSpot create failed")
            status = SyncStatus.CREATED
        else:
            contact_id = existing["id"]
            updated = self._hs.update_contact(
                contact_id=contact_id,
                existing=existing,
                first_name=sender.first_name,
                last_name=sender.last_name,
                company=sender.company,
            )
            status = SyncStatus.UPDATED if updated else SyncStatus.SKIPPED

        if log_activity and contact_id:
            self._hs.log_email_activity(
                contact_id=contact_id,
                email=email,
                subject=sender.subject,
                message_id=sender.message_id,
            )

        return SyncResult(status, email, contact_id)
