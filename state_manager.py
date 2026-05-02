from __future__ import annotations

import json
import os
from typing import Optional


class StateManager:
    """Persists the Gmail historyId and the set of already-processed message IDs."""

    _MAX_PROCESSED = 10_000   # cap to avoid unbounded growth
    _TRIM_TO = 5_000

    def __init__(self, state_file: str) -> None:
        self._path = state_file
        self._state: dict = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path) as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        with open(self._path, "w") as fh:
            json.dump(self._state, fh, indent=2)

    # ------------------------------------------------------------------
    # Gmail history ID
    # ------------------------------------------------------------------

    def get_history_id(self) -> Optional[str]:
        return self._state.get("history_id")

    def set_history_id(self, history_id: str) -> None:
        self._state["history_id"] = history_id
        self._save()

    # ------------------------------------------------------------------
    # Processed message IDs (deduplication)
    # ------------------------------------------------------------------

    def get_processed_messages(self) -> set[str]:
        return set(self._state.get("processed_messages", []))

    def mark_processed(self, message_id: str) -> None:
        processed = self.get_processed_messages()
        processed.add(message_id)
        if len(processed) > self._MAX_PROCESSED:
            processed = set(sorted(processed)[-self._TRIM_TO:])
        self._state["processed_messages"] = list(processed)
        self._save()
