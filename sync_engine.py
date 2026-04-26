from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from config import IGNORED_SENDER_PATTERNS, POLL_INTERVAL_SECONDS, STATE_FILE
from contact_parser import parse_contact
from gmail_client import GmailClient, HistoryIdExpiredError
from hubspot_client import HubSpotClient


@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored" | "error"
    email: str
    contact_id: str = ""
    reason: str = ""


class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient) -> None:
        self._gmail = gmail
        self._hubspot = hubspot
        self._state_path = Path(STATE_FILE)
        self._state: dict = self._load_state()

    # ------------------------------------------------------------------
    # Persistent state (historyId)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        if self._state_path.exists():
            with open(self._state_path) as fh:
                return json.load(fh)
        return {}

    def _save_state(self) -> None:
        with open(self._state_path, "w") as fh:
            json.dump(self._state, fh, indent=2)

    def _history_id(self) -> str | None:
        return self._state.get("history_id")

    def _set_history_id(self, hid: str) -> None:
        self._state["history_id"] = hid
        self._save_state()

    # ------------------------------------------------------------------
    # Single poll cycle
    # ------------------------------------------------------------------

    def run_once(self) -> list[SyncResult]:
        """Fetch new inbox messages and sync them to HubSpot.

        On the very first call the history ID is bootstrapped and an empty
        list is returned (existing mail is not processed retroactively).
        """
        stored_id = self._history_id()

        if not stored_id:
            bootstrap = self._gmail.get_profile_history_id()
            self._set_history_id(bootstrap)
            print(f"[Sync] Initialised — watching from history ID {bootstrap}")
            return []

        try:
            messages = self._gmail.get_new_messages(stored_id)
        except HistoryIdExpiredError:
            # historyId has expired (> 7 days old); reinitialise without data loss
            new_id = self._gmail.get_profile_history_id()
            self._set_history_id(new_id)
            print(f"[Sync] History ID expired — reinitialised to {new_id}")
            return []

        # Advance the cursor regardless of how many messages were processed
        self._set_history_id(self._gmail.get_profile_history_id())

        results: list[SyncResult] = []
        for msg in messages:
            result = self._process_message(msg)
            results.append(result)
            _print_result(result)

        return results

    # ------------------------------------------------------------------
    # Continuous loop
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        print(
            f"[Sync] Gmail → HubSpot sync started "
            f"(polling every {POLL_INTERVAL_SECONDS}s). Press Ctrl-C to stop."
        )
        while True:
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\n[Sync] Stopped.")
                return
            except Exception as exc:
                print(f"[Sync] Unhandled error: {exc}")
            time.sleep(POLL_INTERVAL_SECONDS)

    # ------------------------------------------------------------------
    # Per-message processing
    # ------------------------------------------------------------------

    def _process_message(self, message: dict) -> SyncResult:
        name, email = GmailClient.extract_sender(message)
        subject = GmailClient.extract_subject(message)

        if not email:
            return SyncResult(status="ignored", email="", reason="missing sender address")

        if _is_automated_sender(email):
            return SyncResult(status="ignored", email=email, reason="automated sender")

        contact_data = parse_contact(name, email)
        existing = self._hubspot.find_contact_by_email(email)

        if existing:
            contact_id: str = existing.id
            self._hubspot.update_contact(contact_id, contact_data, existing)
            self._hubspot.add_email_activity(contact_id, email, subject)
            return SyncResult(status="updated", email=email, contact_id=contact_id)

        created = self._hubspot.create_contact(contact_data)
        if created:
            self._hubspot.add_email_activity(created.id, email, subject)
            return SyncResult(status="created", email=email, contact_id=created.id)

        return SyncResult(status="error", email=email, reason="create returned None (possible duplicate race)")


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _is_automated_sender(email: str) -> bool:
    lower = email.lower()
    return any(pat in lower for pat in IGNORED_SENDER_PATTERNS)


_STATUS_ICON = {
    "created": "[CREATED ]",
    "updated": "[UPDATED ]",
    "ignored": "[IGNORED ]",
    "error":   "[ERROR   ]",
}


def _print_result(result: SyncResult) -> None:
    icon = _STATUS_ICON.get(result.status, "[?       ]")
    line = f"{icon} {result.email or '(no email)'}"
    if result.contact_id:
        line += f"  |  HubSpot ID: {result.contact_id}"
    if result.reason:
        line += f"  |  {result.reason}"
    print(line)
