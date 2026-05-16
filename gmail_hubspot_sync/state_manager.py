"""Persists sync state (Gmail historyId) so the poller resumes after restart."""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_file: str) -> None:
        self._file = state_file
        self._state: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as exc:
                logger.warning("Could not read state file (%s); starting fresh.", exc)
        return {}

    def _save(self) -> None:
        with open(self._file, "w") as f:
            json.dump(self._state, f, indent=2)

    @property
    def history_id(self) -> Optional[str]:
        return self._state.get("historyId")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._state["historyId"] = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed", {})

    def mark_processed(self, message_id: str, result: dict) -> None:
        self._state.setdefault("processed", {})[message_id] = result
        self._save()
