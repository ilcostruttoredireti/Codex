"""
Core sync engine: polls Gmail history, deduplicates, and upserts contacts in HubSpot.
State (last processed historyId) is persisted to a JSON file between runs.
"""
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Optional

from config import Config
from gmail_client import GmailClient, SenderInfo
from hubspot_client import ContactResult, HubSpotClient

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored" | "error"
    email: str
    contact_id: Optional[str]
    error: Optional[str] = None

    def __str__(self) -> str:
        status_icon = {"created": "+", "updated": "~", "ignored": "=", "error": "!"}.get(
            self.status, "?"
        )
        parts = [f"[{status_icon}] {self.status.upper():8s}  {self.email}"]
        if self.contact_id:
            parts.append(f"  id={self.contact_id}")
        if self.error:
            parts.append(f"  error={self.error}")
        return "".join(parts)


class SyncEngine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.gmail = GmailClient(config)
        self.hubspot = HubSpotClient(config)
        self._seen_message_ids: set[str] = set()   # in-memory dedup within a run

    # ------------------------------------------------------------------
    # State persistence (last historyId)
    # ------------------------------------------------------------------

    def _load_state(self) -> Optional[str]:
        if os.path.exists(self.config.state_file):
            with open(self.config.state_file) as f:
                return json.load(f).get("history_id")
        return None

    def _save_state(self, history_id: str) -> None:
        with open(self.config.state_file, "w") as f:
            json.dump({"history_id": history_id}, f)

    # ------------------------------------------------------------------
    # Single email processing
    # ------------------------------------------------------------------

    def _process_message(self, message_id: str) -> SyncResult:
        if message_id in self._seen_message_ids:
            return SyncResult(status="ignored", email="", contact_id=None)

        self._seen_message_ids.add(message_id)

        sender = self.gmail.get_message_details(message_id)
        if sender is None:
            return SyncResult(status="ignored", email="", contact_id=None)

        try:
            result: ContactResult = self.hubspot.sync_sender(sender, add_engagement=True)
            return SyncResult(
                status=result.status,
                email=result.email,
                contact_id=result.contact_id,
            )
        except Exception as exc:
            logger.exception("Error syncing sender %s", sender.email)
            return SyncResult(
                status="error",
                email=sender.email,
                contact_id=None,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Single poll cycle
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        """Fetch new messages and process them. Returns all non-trivial results."""
        history_id = self._load_state()

        if history_id is None:
            # First run — record current position without processing backlog
            history_id = self.gmail.get_current_history_id()
            self._save_state(history_id)
            logger.info("First run — starting from historyId=%s (no backlog processed)", history_id)
            return []

        messages = self.gmail.get_new_messages(since_history_id=history_id)
        new_history_id = self.gmail.get_current_history_id()

        results: list[SyncResult] = []
        for msg in messages:
            r = self._process_message(msg["id"])
            if r.email:  # skip empty/ignored
                results.append(r)

        self._save_state(new_history_id)
        return results

    # ------------------------------------------------------------------
    # Continuous polling loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        self.gmail.authenticate()
        logger.info(
            "Starting Gmail → HubSpot sync (poll every %ds)", self.config.poll_interval_seconds
        )

        while True:
            try:
                results = self.run_once()
                for r in results:
                    print(r)
                if results:
                    logger.info("Processed %d message(s) this cycle", len(results))
            except KeyboardInterrupt:
                logger.info("Interrupted by user — stopping")
                break
            except Exception:
                logger.exception("Unexpected error in sync cycle — will retry next poll")

            time.sleep(self.config.poll_interval_seconds)
