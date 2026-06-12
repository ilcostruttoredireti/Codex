"""Core sync loop: Gmail inbox → HubSpot contacts."""
import logging
import time
from dataclasses import dataclass

from contact_extractor import extract_contact
from gmail_reader import GmailReader
from hubspot_manager import HubSpotManager
import config

log = logging.getLogger(__name__)

NOTE_TEMPLATE = (
    "📧 Email ricevuta via Gmail\n"
    "Mittente: {email}\n"
    "Tag: Inbound Gmail\n"
    "Fonte contatto: Gmail"
)


@dataclass
class SyncResult:
    email: str
    status: str       # created | updated | ignored | error
    contact_id: str = ""
    reason: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.upper():8s}]  {self.email:<40}  ID: {self.contact_id or 'N/A'}"]
        if self.reason:
            parts.append(f"  ({self.reason})")
        return "".join(parts)


class SyncEngine:
    def __init__(self):
        self.gmail = GmailReader()
        self.hubspot = HubSpotManager()
        self._label_id: str | None = None

    def _label_id_cached(self) -> str | None:
        if self._label_id is None:
            self._label_id = self.gmail.get_or_create_label(config.GMAIL_PROCESSED_LABEL)
        return self._label_id

    # ── single message ────────────────────────────────────────────────────────

    def _process_one(self, message_id: str) -> SyncResult:
        from_header = self.gmail.get_from_header(message_id)
        if not from_header:
            return SyncResult(email="", status="ignored", reason="no From header")

        contact = extract_contact(from_header)
        if not contact:
            return SyncResult(email=from_header, status="ignored", reason="automated sender")

        email = contact["email"]
        existing = self.hubspot.find_by_email(email)

        if existing is None:
            contact_id = self.hubspot.create_contact(contact)
            if not contact_id:
                return SyncResult(email=email, status="error", reason="HubSpot create failed")
            self.hubspot.create_note(contact_id, NOTE_TEMPLATE.format(email=email))
            return SyncResult(email=email, status="created", contact_id=contact_id)

        contact_id = existing["id"]
        updated = self.hubspot.update_contact(contact_id, contact, existing["properties"])
        if updated:
            return SyncResult(email=email, status="updated", contact_id=contact_id)
        return SyncResult(
            email=email, status="ignored", contact_id=contact_id, reason="already up to date"
        )

    # ── single pass ───────────────────────────────────────────────────────────

    def run_once(self) -> list[SyncResult]:
        messages = self.gmail.get_unprocessed_messages()
        if not messages:
            log.info("No new messages.")
            return []

        log.info("Processing %d messages…", len(messages))
        label_id = self._label_id_cached()
        results: list[SyncResult] = []

        for msg in messages:
            result = self._process_one(msg["id"])
            results.append(result)
            log.info("%s", result)

            # Mark processed (even if ignored) so it is not re-evaluated
            if result.status != "error" and label_id:
                self.gmail.mark_processed(msg["id"], label_id)

        return results

    # ── continuous loop ───────────────────────────────────────────────────────

    def run_loop(self):
        log.info(
            "Gmail → HubSpot sync started (interval: %ss)", config.POLL_INTERVAL_SECONDS
        )
        while True:
            try:
                results = self.run_once()
                if results:
                    created = sum(1 for r in results if r.status == "created")
                    updated = sum(1 for r in results if r.status == "updated")
                    ignored = sum(1 for r in results if r.status in ("ignored",))
                    errors = sum(1 for r in results if r.status == "error")
                    log.info(
                        "Pass done — created:%d  updated:%d  ignored:%d  errors:%d",
                        created, updated, ignored, errors,
                    )
            except Exception:
                log.exception("Unexpected error in sync loop")

            time.sleep(config.POLL_INTERVAL_SECONDS)
