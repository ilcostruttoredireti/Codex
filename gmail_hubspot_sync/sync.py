"""Core sync logic: ties Gmail and HubSpot together."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .gmail_client import GmailClient, SenderInfo
from .hubspot_client import HubSpotClient
from .state import SyncState

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    outcome: Outcome
    contact_email: str
    hubspot_id: Optional[str]
    message: str = ""


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        state: SyncState,
        owner_id: Optional[int] = None,
    ) -> None:
        self._gmail = gmail
        self._hs = hubspot
        self._state = state
        self._owner_id = owner_id

    # ── Public API ────────────────────────────────────────────────────────────

    def run_once(self) -> list[SyncResult]:
        """Process all un-synced inbox messages once and return results."""
        results: list[SyncResult] = []

        # Use epoch of last run to limit Gmail query
        after_ts: Optional[str] = None
        if self._state.last_run_utc:
            after_ts = str(int(self._state.last_run_utc.timestamp()))

        for sender in self._gmail.new_senders(after_timestamp=after_ts):
            if self._state.is_processed(sender.message_id):
                continue

            result = self._process_sender(sender)
            results.append(result)

            # Mark as processed regardless of outcome (avoid re-trying failed ones in same run)
            self._state.mark_processed(sender.message_id)
            self._state.increment(
                {
                    Outcome.CREATED: "created",
                    Outcome.UPDATED: "updated",
                    Outcome.SKIPPED: "skipped",
                    Outcome.ERROR: "errors",
                }[result.outcome]
            )

            # Apply Gmail label
            if result.outcome in (Outcome.CREATED, Outcome.UPDATED):
                self._gmail.apply_synced_label(sender.message_id)

            self._log_result(result)

        self._state.touch()
        self._state.save()
        return results

    # ── Internal ──────────────────────────────────────────────────────────────

    def _process_sender(self, sender: SenderInfo) -> SyncResult:
        try:
            existing = self._hs.find_contact_by_email(sender.email)
        except Exception as exc:
            log.error("HubSpot search failed for %s: %s", sender.email, exc)
            return SyncResult(Outcome.ERROR, sender.email, None, str(exc))

        if existing:
            return self._handle_existing(existing, sender)
        return self._handle_new(sender)

    def _handle_existing(self, contact: dict, sender: SenderInfo) -> SyncResult:
        contact_id = contact["id"]
        existing_props = contact.get("properties", {})

        try:
            changed = self._hs.update_contact_missing_fields(
                contact_id=contact_id,
                existing_props=existing_props,
                firstname=sender.firstname,
                lastname=sender.lastname,
                company=sender.company,
            )
            self._hs.add_inbound_gmail_note(
                contact_id=contact_id,
                sender_email=sender.email,
                subject=sender.subject,
                received_at=sender.received_at,
                owner_id=self._owner_id,
            )
        except Exception as exc:
            log.error("HubSpot update failed for %s (id=%s): %s", sender.email, contact_id, exc)
            return SyncResult(Outcome.ERROR, sender.email, contact_id, str(exc))

        msg = "campi aggiornati" if changed else "nota aggiunta, nessun campo mancante"
        return SyncResult(Outcome.UPDATED, sender.email, contact_id, msg)

    def _handle_new(self, sender: SenderInfo) -> SyncResult:
        try:
            created = self._hs.create_contact(
                email=sender.email,
                firstname=sender.firstname,
                lastname=sender.lastname,
                company=sender.company,
            )
            contact_id = created["id"]
            self._hs.add_inbound_gmail_note(
                contact_id=contact_id,
                sender_email=sender.email,
                subject=sender.subject,
                received_at=sender.received_at,
                owner_id=self._owner_id,
            )
        except Exception as exc:
            log.error("HubSpot create failed for %s: %s", sender.email, exc)
            return SyncResult(Outcome.ERROR, sender.email, None, str(exc))

        return SyncResult(Outcome.CREATED, sender.email, contact_id)

    @staticmethod
    def _log_result(r: SyncResult) -> None:
        icon = {"Creato": "✅", "Aggiornato": "🔄", "Ignorato": "⏭", "Errore": "❌"}.get(
            r.outcome.value, "•"
        )
        log.info(
            "%s %-12s | %-40s | HubSpot ID: %s%s",
            icon,
            r.outcome.value,
            r.contact_email,
            r.hubspot_id or "—",
            f"  ({r.message})" if r.message else "",
        )
