import json
import os
from typing import Optional


class StateManager:
    """Persists sync state (processed message IDs, last Gmail history ID) to disk."""

    _MAX_STORED_IDS = 10_000

    def __init__(self, state_file: str):
        self._file = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._file):
            with open(self._file) as fh:
                return json.load(fh)
        return {"processed_ids": [], "last_history_id": None}

    def _save(self) -> None:
        with open(self._file, "w") as fh:
            json.dump(self._state, fh, indent=2)

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state["processed_ids"]

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._state["processed_ids"]
        if message_id not in ids:
            ids.append(message_id)
            if len(ids) > self._MAX_STORED_IDS:
                self._state["processed_ids"] = ids[-self._MAX_STORED_IDS :]
        self._save()

    @property
    def last_history_id(self) -> Optional[str]:
        return self._state.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str) -> None:
        self._state["last_history_id"] = value
        self._save()
