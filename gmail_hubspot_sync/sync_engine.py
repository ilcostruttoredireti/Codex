"""Orchestrates Gmail → HubSpot contact sync with deduplication."""

import json
import logging
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .contact_parser import SenderContact, parse_sender
from .gmail_client import GmailClient, HistoryExpiredError
from .hubspot_client import HubSpotClient

log = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state_file: str,
        poll_interval: int = 60,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._state_file = Path(state_file)
        self._poll_interval = poll_interval
        self._history_id: str | None = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Continuously poll Gmail and sync contacts to HubSpot."""
        self._gmail.authenticate()
        self._history_id = self._load_state() or self._gmail.get_current_history_id()
        log.info("Sync avviato. history_id iniziale: %s", self._history_id)

        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                log.info("Sync interrotto dall'utente.")
                break
            except Exception as exc:
                log.error("Errore inatteso: %s", exc, exc_info=True)

            time.sleep(self._poll_interval)

    def _tick(self) -> None:
        try:
            messages = self._gmail.fetch_new_messages(self._history_id)
        except HistoryExpiredError:
            log.warning("historyId scaduto, reset al corrente.")
            self._history_id = self._gmail.get_current_history_id()
            self._save_state(self._history_id)
            return

        for msg in messages:
            result = self._process_message(msg)
            if result:
                self._print_result(result)

        # Advance history cursor regardless of whether we processed anything.
        new_id = self._gmail.get_current_history_id()
        if new_id != self._history_id:
            self._history_id = new_id
            self._save_state(new_id)

    # ------------------------------------------------------------------
    # Per-message processing
    # ------------------------------------------------------------------

    def _process_message(self, message: dict) -> SyncResult | None:
        sender = parse_sender(message)
        if sender is None:
            return None

        # Skip no-reply and automated senders.
        if _is_automated(sender.email):
            log.debug("Ignorato (automatico): %s", sender.email)
            return None

        existing = self._hubspot.find_contact_by_email(sender.email)

        if existing is None:
            contact_id = self._create_contact(sender)
            return SyncResult(SyncStatus.CREATED, sender.email, contact_id)

        contact_id = existing["id"]
        updates = self._hubspot.fill_missing_fields(
            contact_id,
            existing["properties"],
            {
                "firstname": sender.firstname,
                "lastname": sender.lastname,
                "company": sender.company,
            },
        )
        if updates:
            self._hubspot.update_contact(contact_id, updates)
            self._log_activity(contact_id, sender)
            return SyncResult(SyncStatus.UPDATED, sender.email, contact_id)

        return SyncResult(SyncStatus.IGNORED, sender.email, contact_id)

    def _create_contact(self, sender: SenderContact) -> str:
        contact_id = self._hubspot.create_contact(
            {
                "email": sender.email,
                "firstname": sender.firstname,
                "lastname": sender.lastname,
                "company": sender.company,
            }
        )
        self._log_activity(contact_id, sender)
        return contact_id

    def _log_activity(self, contact_id: str, sender: SenderContact) -> None:
        self._hubspot.log_email_activity(contact_id, sender.subject, sender.date)

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> str | None:
        if self._state_file.exists():
            try:
                data = json.loads(self._state_file.read_text())
                return data.get("history_id")
            except (json.JSONDecodeError, KeyError):
                pass
        return None

    def _save_state(self, history_id: str) -> None:
        self._state_file.write_text(json.dumps({"history_id": history_id}))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    @staticmethod
    def _print_result(result: SyncResult) -> None:
        print(
            f"[{result.status.value:10s}] email={result.email}  "
            f"hubspot_id={result.contact_id}"
        )
        log.info(
            "status=%s email=%s contact_id=%s",
            result.status.value,
            result.email,
            result.contact_id,
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

_AUTOMATED_PREFIXES = ("noreply", "no-reply", "donotreply", "mailer-daemon", "postmaster")


def _is_automated(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(local.startswith(p) for p in _AUTOMATED_PREFIXES)
