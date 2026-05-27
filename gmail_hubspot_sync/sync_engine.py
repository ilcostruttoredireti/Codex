"""Orchestrates Gmail polling → contact extraction → HubSpot sync."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from contact_extractor import SenderContact, extract_sender
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import load_history_id, save_history_id

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str] = None
    detail: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.email}"]
        if self.hubspot_id:
            parts.append(f"HubSpot ID: {self.hubspot_id}")
        if self.detail:
            parts.append(f"({self.detail})")
        return "  ".join(parts)


class SyncEngine:
    def __init__(self):
        self._gmail = GmailClient()
        self._hubspot = HubSpotClient()

    # ------------------------------------------------------------------
    # Main entry point called by the polling loop
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        history_id = load_history_id()

        if history_id is None:
            # First run — seed the state and exit without processing old mail
            current_id = self._gmail.get_history_id()
            save_history_id(current_id)
            logger.info("First run: seeded historyId=%s. Next poll will process new mail.", current_id)
            return []

        results: list[SyncResult] = []
        seen_emails: set[str] = set()  # dedupe within a single batch
        latest_history_id = history_id

        for message in self._gmail.iter_new_messages(history_id):
            # Track the most recent historyId from fetched messages
            msg_history_id = message.get("historyId")
            if msg_history_id and int(msg_history_id) > int(latest_history_id):
                latest_history_id = msg_history_id

            sender = extract_sender(message)
            if sender is None:
                continue

            if sender.email in seen_emails:
                continue
            seen_emails.add(sender.email)

            result = self._sync_contact(sender)
            results.append(result)
            logger.info(str(result))

        save_history_id(latest_history_id)
        return results

    # ------------------------------------------------------------------
    # Contact sync logic
    # ------------------------------------------------------------------

    def _sync_contact(self, sender: SenderContact) -> SyncResult:
        try:
            existing = self._hubspot.find_by_email(sender.email)
            new_props = self._hubspot.build_props(
                email=sender.email,
                first_name=sender.first_name,
                last_name=sender.last_name,
                company=sender.company,
            )

            if existing is None:
                created = self._hubspot.create_contact(new_props)
                if created:
                    return SyncResult(
                        status=SyncStatus.CREATED,
                        email=sender.email,
                        hubspot_id=created.get("id"),
                    )
                return SyncResult(
                    status=SyncStatus.ERROR,
                    email=sender.email,
                    detail="create returned None",
                )

            # Contact exists — fill in missing fields only
            updates = self._hubspot.merge_missing_fields(existing, new_props)
            if updates:
                updated = self._hubspot.update_contact(existing["id"], updates)
                return SyncResult(
                    status=SyncStatus.UPDATED,
                    email=sender.email,
                    hubspot_id=existing["id"],
                    detail=f"updated fields: {list(updates.keys())}",
                )

            return SyncResult(
                status=SyncStatus.IGNORED,
                email=sender.email,
                hubspot_id=existing["id"],
                detail="no new fields to update",
            )

        except Exception as exc:
            logger.exception("Unexpected error syncing %s", sender.email)
            return SyncResult(
                status=SyncStatus.ERROR,
                email=sender.email,
                detail=str(exc),
            )
