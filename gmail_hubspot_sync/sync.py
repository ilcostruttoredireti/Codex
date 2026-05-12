"""
Core sync loop: polls Gmail, resolves each sender against HubSpot,
creates/updates contacts, and marks emails as processed.
"""

import logging
import time

from . import config
from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import HubSpotClient, SyncResult, SyncStatus

logger = logging.getLogger(__name__)


class GmailHubSpotSync:
    def __init__(self) -> None:
        self.gmail = GmailClient()
        self.hubspot = HubSpotClient()

    # ------------------------------------------------------------------
    # Bootstrap
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Run Gmail OAuth2 flow (interactive on first use)."""
        self.gmail.authenticate()

    # ------------------------------------------------------------------
    # Single poll cycle
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        """
        Fetch new emails, sync contacts to HubSpot, mark emails as processed.
        Returns the list of SyncResult for this cycle.
        """
        logger.info("Polling Gmail for new messages...")
        senders = self.gmail.fetch_new_messages()

        if not senders:
            logger.info("No new messages to process.")
            return []

        logger.info("Processing %d sender(s).", len(senders))
        results: list[SyncResult] = []

        for sender in senders:
            result = self._sync_sender(sender)
            results.append(result)
            print(result)  # real-time output for CLI use
            # Mark the message regardless of sync outcome so we don't re-process
            if sender.message_id:
                self.gmail.mark_as_processed(sender.message_id)

        _log_summary(results)
        return results

    # ------------------------------------------------------------------
    # Continuous loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """Poll Gmail every POLL_INTERVAL_SECONDS indefinitely."""
        logger.info(
            "Starting continuous sync (interval=%ds).", config.POLL_INTERVAL_SECONDS
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                logger.info("Sync stopped by user.")
                break
            except Exception as exc:
                logger.exception("Unexpected error during sync cycle: %s", exc)

            logger.info(
                "Next poll in %d seconds...", config.POLL_INTERVAL_SECONDS
            )
            time.sleep(config.POLL_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Per-sender logic
    # ------------------------------------------------------------------

    def _sync_sender(self, info: SenderInfo) -> SyncResult:
        """Decide whether to create or update a HubSpot contact for *info*."""
        existing = self.hubspot.find_contact_by_email(info.email)

        if existing is None:
            result = self.hubspot.create_contact(info)
            if result.status == SyncStatus.CREATED:
                self.hubspot.log_email_activity(result.contact_id, info)
        else:
            result = self.hubspot.update_contact(existing, info)
            if result.status == SyncStatus.UPDATED:
                self.hubspot.log_email_activity(result.contact_id, info)

        return result


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _log_summary(results: list[SyncResult]) -> None:
    created = sum(1 for r in results if r.status == SyncStatus.CREATED)
    updated = sum(1 for r in results if r.status == SyncStatus.UPDATED)
    ignored = sum(1 for r in results if r.status == SyncStatus.IGNORED)
    logger.info(
        "Ciclo completato — Creati: %d | Aggiornati: %d | Ignorati: %d",
        created,
        updated,
        ignored,
    )
