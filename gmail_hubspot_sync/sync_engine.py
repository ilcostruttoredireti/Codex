"""Core sync engine: ties Gmail, HubSpot, and state tracking together."""

import logging
import time
from typing import List

from .config import AppConfig
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient
from .models import SyncResult, SyncStatus
from .state import ProcessedMessageTracker

logger = logging.getLogger(__name__)


class SyncEngine:
    """
    Orchestrates the Gmail → HubSpot contact sync loop.

    Flow:
      1. Fetch new Gmail messages not yet in state.
      2. Extract sender contact info.
      3. Sync contact to HubSpot (create/update/ignore).
      4. Mark message as processed.
      5. Print sync result.
      6. Sleep for poll_interval seconds, then repeat.
    """

    def __init__(self, config: AppConfig):
        self.config = config
        self.gmail = GmailClient(config.gmail)
        self.hubspot = HubSpotClient(config.hubspot)
        self.tracker = ProcessedMessageTracker(config.state_file)

    # ------------------------------------------------------------------ cycle

    def run_cycle(self) -> List[SyncResult]:
        """Execute one poll cycle. Returns list of SyncResults."""
        results: List[SyncResult] = []

        messages = list(
            self.gmail.get_new_messages(
                processed_ids=self.tracker.ids,
                label=self.config.gmail.label,
            )
        )

        if not messages:
            logger.debug("Nessun nuovo messaggio in questo ciclo.")
            return results

        logger.info("=== Ciclo sync: %d nuovi messaggi ===", len(messages))

        for message in messages:
            msg_id = message.get("_parsed_id", message.get("id", "unknown"))
            subject = self.gmail.get_subject(message)

            # Extract contact
            contact = self.gmail.extract_contact(message)
            if contact is None:
                logger.warning("Messaggio %s: impossibile estrarre contatto — saltato", msg_id)
                self.tracker.mark_processed(msg_id)
                continue

            # Skip no-reply / system addresses
            if self._is_system_address(contact.email):
                logger.debug("Indirizzo di sistema ignorato: %s", contact.email)
                result = SyncResult(
                    status=SyncStatus.IGNORED,
                    email=contact.email,
                    message_id=msg_id,
                    subject=subject,
                )
                results.append(result)
                self.tracker.mark_processed(msg_id)
                continue

            # Sync to HubSpot
            try:
                result = self.hubspot.sync_contact(contact)
            except Exception as exc:
                logger.error("Errore sync HubSpot per %s: %s", contact.email, exc)
                result = SyncResult(
                    status=SyncStatus.ERROR,
                    email=contact.email,
                    error=str(exc),
                )

            result.message_id = msg_id
            result.subject = subject
            results.append(result)

            # Always mark as processed to avoid infinite retry loops
            self.tracker.mark_processed(msg_id)
            self._print_result(result)

        return results

    # ---------------------------------------------------------------- loop

    def run(self):
        """
        Main entry point.

        If config.run_once is True → execute one cycle and exit.
        Otherwise → loop indefinitely with poll_interval sleep between cycles.
        """
        logger.info(
            "Gmail→HubSpot Sync avviato | intervallo: %ds | label: %s",
            self.config.gmail.poll_interval,
            self.config.gmail.label,
        )

        if self.config.run_once:
            self.run_cycle()
            logger.info("RUN_ONCE abilitato — uscita.")
            return

        while True:
            try:
                self.run_cycle()
            except KeyboardInterrupt:
                logger.info("Interruzione manuale — uscita.")
                break
            except Exception as exc:
                logger.error("Errore imprevisto nel ciclo: %s", exc, exc_info=True)

            logger.debug("Attendo %ds per il prossimo ciclo...", self.config.gmail.poll_interval)
            try:
                time.sleep(self.config.gmail.poll_interval)
            except KeyboardInterrupt:
                logger.info("Interruzione durante sleep — uscita.")
                break

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _is_system_address(email: str) -> bool:
        """Return True for addresses that should never become CRM contacts."""
        system_prefixes = (
            "noreply", "no-reply", "donotreply", "do-not-reply",
            "mailer-daemon", "postmaster", "bounce", "bounces",
            "notifications", "notification", "autoresponder",
            "unsubscribe",
        )
        local = email.split("@")[0].lower()
        return any(local.startswith(p) for p in system_prefixes)

    @staticmethod
    def _print_result(result: SyncResult):
        """Print a formatted sync result line to stdout."""
        icons = {
            SyncStatus.CREATED: "✅",
            SyncStatus.UPDATED: "🔄",
            SyncStatus.IGNORED: "⏭️",
            SyncStatus.ERROR: "❌",
        }
        icon = icons.get(result.status, "❓")
        parts = [
            f"{icon} Stato: {result.status.value:<10}",
            f"Email: {result.email}",
        ]
        if result.hubspot_contact_id:
            parts.append(f"ID HubSpot: {result.hubspot_contact_id}")
        if result.subject:
            subject_short = result.subject[:50] + "…" if len(result.subject) > 50 else result.subject
            parts.append(f"Oggetto: «{subject_short}»")
        if result.error:
            parts.append(f"⚠️  {result.error}")
        print(" | ".join(parts))
