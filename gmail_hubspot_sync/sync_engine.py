import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .config import Config
from .contact_extractor import SenderContact, extract_sender
from .state_manager import StateManager

if False:  # type-checking only
    from .gmail_client import GmailClient
    from .hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str]
    reason: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.email}"]
        if self.hubspot_id:
            parts.append(f"HubSpot ID: {self.hubspot_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


class SyncEngine:
    def __init__(
        self,
        gmail,
        hubspot,
        state: StateManager,
    ):
        self.gmail = gmail
        self.hubspot = hubspot
        self.state = state

    def run_once(self) -> list[SyncResult]:
        """Process all new inbox emails since the last run. Returns results."""
        since = self.state.last_timestamp
        logger.info("Fetching inbox messages since %s", since or "yesterday")

        results: list[SyncResult] = []
        latest_dt: Optional[datetime] = None

        for message in self.gmail.get_inbox_messages(since=since):
            msg_id = message.get("id", "")

            if self.state.is_processed(msg_id):
                continue

            headers = self.gmail.extract_headers(message)
            from_header = headers.get("from", "")
            if not from_header:
                self.state.mark_processed(msg_id)
                continue

            result = self._process_sender(from_header)
            results.append(result)
            self.state.mark_processed(msg_id)

            # Track latest message date for state update
            date_header = headers.get("date", "")
            msg_dt = _parse_date_header(date_header)
            if msg_dt and (latest_dt is None or msg_dt > latest_dt):
                latest_dt = msg_dt

        if latest_dt:
            self.state.set_last_timestamp(latest_dt)
        elif since is None:
            # First run with no messages: record now so next run only fetches new
            self.state.set_last_timestamp(datetime.now(timezone.utc))

        return results

    def _process_sender(self, from_header: str) -> SyncResult:
        sender = extract_sender(from_header)

        if sender is None:
            return SyncResult(SyncStatus.IGNORED, from_header, None, "invalid address")

        if _should_skip(sender):
            return SyncResult(SyncStatus.IGNORED, sender.email, None, "automated sender")

        existing = self.hubspot.find_contact_by_email(sender.email)

        if existing:
            contact_id = str(existing["id"])
            updated = self.hubspot.update_contact(contact_id, sender, existing)
            if updated and updated != existing:
                return SyncResult(SyncStatus.UPDATED, sender.email, contact_id)
            return SyncResult(SyncStatus.IGNORED, sender.email, contact_id, "no new fields")

        created = self.hubspot.create_contact(sender)
        if created:
            return SyncResult(SyncStatus.CREATED, sender.email, str(created["id"]))

        return SyncResult(SyncStatus.IGNORED, sender.email, None, "HubSpot error")


def _should_skip(sender: SenderContact) -> bool:
    local = sender.email.split("@")[0]
    if local in Config.SKIP_PREFIXES:
        return True
    if sender.domain in Config.SKIP_DOMAINS:
        return True
    for prefix in Config.SKIP_PREFIXES:
        if local.startswith(prefix):
            return True
    return False


def _parse_date_header(date_str: str) -> Optional[datetime]:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc)
    except Exception:
        return None
