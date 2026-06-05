"""Core sync loop: poll Gmail → parse sender → upsert HubSpot contact."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .contact_parser import parse_from_header
from .gmail_client import GmailClient, HistoryExpiredError
from .hubspot_client import HubSpotClient, SyncResult, SyncStatus

logger = logging.getLogger(__name__)

# Domains to skip (the account owner's own domain, notifications, etc.)
_SKIP_DOMAINS = {"noreply.com", "no-reply.com", "mailer-daemon.google.com"}
_SKIP_PREFIXES = ("noreply@", "no-reply@", "mailer-daemon@", "postmaster@", "notifications@")


def _should_skip(email: str) -> bool:
    low = email.lower()
    if any(low.startswith(p) for p in _SKIP_PREFIXES):
        return True
    domain = low.split("@", 1)[-1] if "@" in low else ""
    return domain in _SKIP_DOMAINS


class GmailHubSpotSync:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state_file: str = ".gmail_sync_state",
        poll_interval: int = 60,
    ) -> None:
        self._gmail = gmail
        self._hubspot = hubspot
        self._state_path = Path(state_file)
        self._poll_interval = poll_interval

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        """Process all new emails since last run. Returns results list."""
        history_id = self._load_history_id()
        results: list[SyncResult] = []

        try:
            for msg in self._gmail.messages_since(history_id):
                result = self._process_message(msg["id"])
                if result:
                    results.append(result)
                    self._log_result(result)
        except HistoryExpiredError:
            logger.warning("History ID expired — resetting cursor to current position.")
            self._save_history_id(self._gmail.get_history_id())
            return results

        # Advance cursor to now so next call only sees newer messages
        new_history_id = self._gmail.get_history_id()
        self._save_history_id(new_history_id)
        return results

    def run_forever(self) -> None:
        """Poll indefinitely, sleeping poll_interval seconds between iterations."""
        logger.info("Starting Gmail → HubSpot sync loop (interval=%ds)", self._poll_interval)
        while True:
            try:
                results = self.run_once()
                if results:
                    logger.info("Processed %d messages this cycle.", len(results))
            except Exception as exc:
                logger.error("Unexpected error during sync: %s", exc, exc_info=True)
            time.sleep(self._poll_interval)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult | None:
        from_header = self._gmail.get_from_header(message_id)
        if not from_header:
            return None

        contact = parse_from_header(from_header)
        if contact is None:
            logger.debug("Could not parse From header: %s", from_header)
            return None

        if _should_skip(contact.email):
            logger.debug("Skipping automated sender: %s", contact.email)
            return None

        return self._hubspot.upsert_contact(
            email=contact.email,
            first_name=contact.first_name,
            last_name=contact.last_name,
            company=contact.company,
        )

    def _load_history_id(self) -> str:
        if self._state_path.exists():
            state = json.loads(self._state_path.read_text())
            return state.get("history_id", "")
        # First run: bootstrap from current position
        history_id = self._gmail.get_history_id()
        self._save_history_id(history_id)
        logger.info("First run — starting from current history ID %s.", history_id)
        return history_id

    def _save_history_id(self, history_id: str) -> None:
        self._state_path.write_text(json.dumps({"history_id": history_id}))

    @staticmethod
    def _log_result(result: SyncResult) -> None:
        icon = {"Creato": "✚", "Aggiornato": "↻", "Ignorato": "–"}.get(
            result.status.value, "?"
        )
        logger.info(
            "%s %-10s  email=%-40s  id=%s",
            icon,
            result.status.value,
            result.email,
            result.contact_id,
        )
