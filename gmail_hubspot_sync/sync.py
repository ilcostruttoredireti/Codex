"""Core sync loop: poll Gmail → upsert contacts in HubSpot."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable

from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    status: str       # "created" | "updated" | "ignored" | "skipped" | "error"
    email: str
    contact_id: str
    message_id: str


class GmailHubSpotSync:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        ignored_domains: set[str] | None = None,
        mark_read: bool = True,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._ignored_domains = ignored_domains or set()
        self._mark_read = mark_read

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def run_once(self, query: str = "is:unread in:inbox") -> list[SyncResult]:
        """Process all currently unread messages and return one result per message."""
        senders = self._gmail.fetch_unread(query)
        results = []
        for sender in senders:
            result = self._process(sender)
            results.append(result)
            _log_result(result)
        return results

    def run_forever(
        self,
        query: str = "is:unread in:inbox",
        poll_interval: int = 60,
    ) -> None:
        """Poll Gmail indefinitely, sleeping poll_interval seconds between passes."""
        log.info("Starting Gmail→HubSpot sync loop (interval=%ds)", poll_interval)
        while True:
            try:
                results = self.run_once(query)
                if results:
                    _print_summary(results)
                else:
                    log.debug("No new messages.")
            except Exception as exc:  # noqa: BLE001
                log.error("Sync pass failed: %s", exc)
            time.sleep(poll_interval)

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _process(self, sender: dict) -> SyncResult:
        message_id = sender["message_id"]
        email = sender["email"]
        domain = sender["domain"]

        if domain in self._ignored_domains:
            return SyncResult("skipped", email, "", message_id)

        try:
            status, contact_id = self._hubspot.upsert_contact(sender)
        except Exception as exc:  # noqa: BLE001
            log.error("HubSpot upsert failed for %s: %s", email, exc)
            return SyncResult("error", email, "", message_id)

        if self._mark_read and status != "error":
            try:
                self._gmail.mark_as_read(message_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not mark message %s as read: %s", message_id, exc)

        return SyncResult(status, email, contact_id, message_id)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _log_result(r: SyncResult) -> None:
    icons = {"created": "✚", "updated": "↻", "ignored": "–", "skipped": "⊘", "error": "✗"}
    icon = icons.get(r.status, "?")
    if r.contact_id:
        log.info("%s %-10s | %-40s | id=%s", icon, r.status.upper(), r.email, r.contact_id)
    else:
        log.info("%s %-10s | %s", icon, r.status.upper(), r.email)


def _print_summary(results: list[SyncResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    parts = ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
    log.info("Pass complete — %d message(s): %s", len(results), parts)
