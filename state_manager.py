import json
import os
from typing import Optional

_MAX_STORED_IDS = 2000


class StateManager:
    """Persists Gmail history ID and processed message IDs to a JSON file."""

    def __init__(self, state_file: str):
        self._path = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path) as fh:
                return json.load(fh)
        return {"history_id": None, "processed_ids": []}

    def _save(self):
        with open(self._path, "w") as fh:
            json.dump(self._state, fh, indent=2)

    # ------------------------------------------------------------------

    def get_history_id(self) -> Optional[str]:
        return self._state.get("history_id")

    def set_history_id(self, history_id: str):
        self._state["history_id"] = history_id
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids", [])

    def mark_processed(self, message_id: str):
        ids: list = self._state.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # Keep a rolling window to prevent unbounded growth
            self._state["processed_ids"] = ids[-_MAX_STORED_IDS:]
            self._save()
