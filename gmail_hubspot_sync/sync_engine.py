import logging
from dataclasses import dataclass
from typing import Optional

from contact_extractor import SenderInfo
from hubspot_client import HubSpotClient
from config import CONTACT_SOURCE

logger = logging.getLogger(__name__)

STATUS_CREATED = "created"
STATUS_UPDATED = "updated"
STATUS_SKIPPED = "skipped"


@dataclass
class SyncResult:
    status: str
    email: str
    contact_id: Optional[str]
    detail: str = ""

    def __str__(self) -> str:
        icons = {STATUS_CREATED: "✅", STATUS_UPDATED: "🔄", STATUS_SKIPPED: "⏭️"}
        icon = icons.get(self.status, "❓")
        return (
            f"{icon} [{self.status.upper():8}] "
            f"{self.email:<40} ID: {self.contact_id or 'N/A'}"
            + (f"  ({self.detail})" if self.detail else "")
        )


class SyncEngine:
    def __init__(self, hs: HubSpotClient):
        self.hs = hs

    def sync(self, sender: SenderInfo) -> SyncResult:
        existing = self.hs.find_by_email(sender.email)

        if existing:
            contact_id = existing["id"]
            props = existing["properties"] or {}
            updates: dict = {}

            if sender.first_name and not props.get("firstname"):
                updates["firstname"] = sender.first_name
            if sender.last_name and not props.get("lastname"):
                updates["lastname"] = sender.last_name
            if sender.company and not props.get("company"):
                updates["company"] = sender.company
            if not props.get("hs_lead_source"):
                updates["hs_lead_source"] = CONTACT_SOURCE

            if updates:
                ok = self.hs.update(contact_id, updates)
                if ok:
                    return SyncResult(
                        STATUS_UPDATED, sender.email, contact_id,
                        f"fields: {list(updates.keys())}"
                    )
            return SyncResult(STATUS_SKIPPED, sender.email, contact_id, "no new fields")

        # New contact
        props = {"email": sender.email, "hs_lead_source": CONTACT_SOURCE}
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company

        new_id = self.hs.create(props)
        if new_id:
            return SyncResult(STATUS_CREATED, sender.email, new_id)
        return SyncResult(STATUS_SKIPPED, sender.email, None, "create failed")
