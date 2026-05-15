"""
Main sync loop: Gmail → HubSpot.
"""

import logging
import time
from typing import Optional

from .contact_extractor import extract_contact
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient

log = logging.getLogger(__name__)

# Senders we never want to sync (notifications, noreply, etc.)
_SKIP_PATTERNS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "notifications", "bounce", "mailer-daemon", "postmaster",
}


def _should_skip(email: str) -> bool:
    local = email.split("@")[0].lower()
    return any(p in local for p in _SKIP_PATTERNS)


class SyncEngine:
    def __init__(
        self,
        gmail: GmailClient,
        hubspot: HubSpotClient,
        poll_interval: int = 60,
    ):
        self._gmail = gmail
        self._hubspot = hubspot
        self._poll_interval = poll_interval

    def run_once(self) -> list[dict]:
        """Process all new emails. Returns list of result dicts."""
        results = []
        for msg in self._gmail.get_new_messages():
            result = self._process_message(msg)
            if result:
                results.append(result)
                self._log_result(result)
        return results

    def run_forever(self) -> None:
        """Poll Gmail continuously."""
        log.info("Starting Gmail → HubSpot sync (interval=%ds)", self._poll_interval)
        while True:
            try:
                results = self.run_once()
                if results:
                    log.info("Processed %d messages this cycle.", len(results))
            except Exception as exc:
                log.error("Sync cycle error: %s", exc, exc_info=True)
            time.sleep(self._poll_interval)

    # ------------------------------------------------------------------

    def _process_message(self, msg: dict) -> Optional[dict]:
        from_header = msg.get("from", "")
        if not from_header:
            return None

        contact = extract_contact(from_header)
        if not contact.get("email"):
            return None

        if _should_skip(contact["email"]):
            return {
                "status": "ignored",
                "email": contact["email"],
                "id": "",
                "reason": "automated sender",
            }

        try:
            result = self._hubspot.sync_contact(contact)
        except Exception as exc:
            log.error("HubSpot error for %s: %s", contact["email"], exc)
            return {
                "status": "error",
                "email": contact["email"],
                "id": "",
                "reason": str(exc),
            }

        result["subject"] = msg.get("subject", "")
        return result

    @staticmethod
    def _log_result(result: dict) -> None:
        status = result.get("status", "?").upper()
        email = result.get("email", "")
        contact_id = result.get("id", "")
        subject = result.get("subject", "")
        icons = {"CREATED": "✚", "UPDATED": "↻", "IGNORED": "–", "ERROR": "✗"}
        icon = icons.get(status, "?")
        msg = f"{icon} [{status}] {email}"
        if contact_id:
            msg += f"  (ID: {contact_id})"
        if subject:
            msg += f'  | "{subject}"'
        log.info(msg)
