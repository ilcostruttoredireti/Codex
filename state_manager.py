"""Persist sync state (last check timestamp + processed message IDs) to disk."""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_IDS = 2_000  # cap to avoid unbounded growth


class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._last_check: Optional[datetime] = None
        self._processed_ids: set = set()
        self._load()

    # ── public interface ──────────────────────────────────────────────────────

    @property
    def last_check(self) -> Optional[datetime]:
        return self._last_check

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed_ids

    def mark_processed(self, message_id: str) -> None:
        self._processed_ids.add(message_id)
        # Trim oldest entries if the set grows too large
        if len(self._processed_ids) > _MAX_IDS * 1.5:
            trimmed = sorted(self._processed_ids)[-_MAX_IDS:]
            self._processed_ids = set(trimmed)

    def update_last_check(self) -> None:
        """Advance the last-check timestamp and persist to disk."""
        self._last_check = datetime.now(timezone.utc)
        self._save()

    # ── private helpers ───────────────────────────────────────────────────────

    def _load(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as fh:
                data = json.load(fh)
            if data.get("last_check"):
                self._last_check = datetime.fromisoformat(data["last_check"])
            self._processed_ids = set(data.get("processed_ids", []))
            logger.debug(
                "State loaded: last_check=%s  processed=%d",
                self._last_check,
                len(self._processed_ids),
            )
        except Exception as exc:
            logger.warning("Could not load state file %s: %s", self.state_file, exc)

    def _save(self) -> None:
        data = {
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "processed_ids": list(self._processed_ids)[-_MAX_IDS:],
        }
        try:
            with open(self.state_file, "w") as fh:
                json.dump(data, fh, indent=2)
        except Exception as exc:
            logger.error("Could not save state to %s: %s", self.state_file, exc)
