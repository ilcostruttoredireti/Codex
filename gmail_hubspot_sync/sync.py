"""
sync.py – core synchronisation logic (one pass over new Gmail messages)
"""
from __future__ import annotations

from collections import Counter
from typing import Optional

from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from logger import get_logger
from models import EmailMessage, SyncResult, SyncStatus

logger = get_logger()


class GmailHubSpotSyncer:
    """
    Orchestrates a single synchronisation pass:

    1.  Fetch unprocessed messages from Gmail
    2.  For each sender:
        a. Search HubSpot for an existing contact
        b. Create or update the contact
        c. Optionally create a timeline activity
    3.  Mark each Gmail message with the processed label
    4.  Return a summary of results
    """

    def __init__(
        self,
        gmail: Optional[GmailClient] = None,
        hubspot: Optional[HubSpotClient] = None,
    ) -> None:
        self.gmail = gmail or GmailClient()
        self.hubspot = hubspot or HubSpotClient()

    def run_once(self, create_activities: bool = True) -> list[SyncResult]:
        """
        Execute one synchronisation pass.

        Returns a list of SyncResult, one per processed message.
        """
        results: list[SyncResult] = []
        # Track emails we already processed in *this* pass to avoid
        # creating/updating the same contact multiple times if the inbox
        # has many messages from the same sender
        seen_emails: set[str] = set()

        for msg in self.gmail.fetch_new_messages():
            result = self._process_message(msg, seen_emails, create_activities)
            result.message_id = msg.message_id
            results.append(result)

            # Always mark the Gmail message as processed, even if
            # the contact was IGNORED (duplicate) or ERROR
            self.gmail.mark_as_processed(msg.message_id)
            seen_emails.add(msg.sender.email.lower())

        self._print_summary(results)
        return results

    # ── Internal helpers ──────────────────────────────────────

    def _process_message(
        self,
        msg: EmailMessage,
        seen_emails: set[str],
        create_activities: bool,
    ) -> SyncResult:
        email = msg.sender.email.lower()

        # Sender already handled in this pass
        if email in seen_emails:
            logger.debug("Sender %s already processed this pass – skipping", email)
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=email,
            )

        logger.info("Processing message from: %s  subject: '%s'", email, msg.subject[:60])

        existing = self.hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            result = self.hubspot.update_contact(
                contact_id=contact_id,
                sender=msg.sender,
                existing_props=existing.get("properties", {}),
            )
        else:
            result = self.hubspot.create_contact(msg.sender)

        # Timeline activity (best-effort)
        if create_activities and result.hubspot_id and result.status != SyncStatus.ERROR:
            self.hubspot.create_email_activity(result.hubspot_id, msg)

        return result

    def _print_summary(self, results: list[SyncResult]) -> None:
        if not results:
            logger.info("Nessun nuovo messaggio da elaborare.")
            return

        counts: Counter = Counter(r.status for r in results)
        logger.info(
            "── Riepilogo sincronizzazione ──────────────────────────────────"
        )
        logger.info(
            "  Messaggi elaborati : %d", len(results)
        )
        logger.info("  ✅ Creati          : %d", counts[SyncStatus.CREATED])
        logger.info("  🔄 Aggiornati      : %d", counts[SyncStatus.UPDATED])
        logger.info("  ⏭  Ignorati        : %d", counts[SyncStatus.IGNORED])
        logger.info("  ❌ Errori          : %d", counts[SyncStatus.ERROR])
        logger.info(
            "────────────────────────────────────────────────────────────────"
        )

        if counts[SyncStatus.ERROR]:
            for r in results:
                if r.status == SyncStatus.ERROR:
                    logger.warning("  Errore per %s: %s", r.contact_email, r.error)
