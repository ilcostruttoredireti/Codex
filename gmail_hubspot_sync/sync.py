"""
Core sync orchestrator: ties Gmail polling to HubSpot upsert logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from gmail_client import GmailClient, SenderInfo
from hubspot_client import HubSpotClient, SyncResult, SyncStatus

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
    created: int = 0
    updated: int = 0
    ignored: int = 0
    errors: int = 0

    @property
    def total(self) -> int:
        return self.created + self.updated + self.ignored + self.errors


# Optional callback signature: receives each SyncResult as it happens
ResultCallback = Callable[[SenderInfo, SyncResult], None]


class GmailHubSpotSync:
    """
    Polls Gmail for new inbound messages and upserts the sender's contact
    in HubSpot.

    Args:
        gmail: Authenticated GmailClient instance.
        hubspot: Authenticated HubSpotClient instance.
        on_result: Optional callback invoked after each email is processed.
        skip_domains: Set of email domains to never sync (e.g. internal domains).
    """

    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        on_result: ResultCallback | None = None,
        skip_domains: set[str] | None = None,
    ) -> None:
        self._gmail = gmail
        self._hubspot = hubspot
        self._on_result = on_result
        self._skip_domains = skip_domains or set()

    def run_once(self) -> SyncStats:
        """
        Process all new emails available right now.
        Returns a SyncStats summary of this batch.
        """
        stats = SyncStats()

        for sender in self._gmail.poll_new_senders():
            if sender.domain in self._skip_domains:
                logger.debug("Skipping domain %s", sender.domain)
                continue

            try:
                result = self._hubspot.upsert_contact(
                    email=sender.email,
                    first_name=sender.first_name,
                    last_name=sender.last_name,
                    company=sender.company,
                    subject=sender.subject,
                    message_id=sender.message_id,
                )
            except Exception as exc:
                logger.error("HubSpot error for %s: %s", sender.email, exc)
                stats.errors += 1
                continue

            _log_result(sender, result)

            if result.status == SyncStatus.CREATED:
                stats.created += 1
            elif result.status == SyncStatus.UPDATED:
                stats.updated += 1
            else:
                stats.ignored += 1

            if self._on_result:
                self._on_result(sender, result)

        return stats


def _log_result(sender: SenderInfo, result: SyncResult) -> None:
    icon = {"Creato": "+", "Aggiornato": "~", "Ignorato": "="}[result.status]
    extra = f" — {result.message}" if result.message else ""
    logger.info(
        "[%s] %s | %s | ID: %s%s",
        icon,
        result.status,
        result.email,
        result.contact_id,
        extra,
    )
