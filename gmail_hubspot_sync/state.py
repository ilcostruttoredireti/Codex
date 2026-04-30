import json
import os
from typing import Optional

_MAX_IDS = 10_000   # cap on stored processed IDs
_TRIM_TO = 5_000    # trim to this size when cap is reached


class State:
    """Persists sync state (last Gmail history ID + processed message IDs)."""

    def __init__(self, state_file: str):
        self._path = state_file
        self._data: dict = self._load()

    # ------------------------------------------------------------------
    # History ID
    # ------------------------------------------------------------------

    @property
    def last_history_id(self) -> Optional[str]:
        return self._data.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str) -> None:
        self._data["last_history_id"] = value
        self._save()

    # ------------------------------------------------------------------
    # Processed message IDs
    # ------------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data["processed_ids"]

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._data["processed_ids"]
        if message_id in ids:
            return
        ids.append(message_id)
        if len(ids) > _MAX_IDS:
            self._data["processed_ids"] = ids[-_TRIM_TO:]
        self._save()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path) as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_history_id": None, "processed_ids": []}

    def _save(self) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._data, fh, indent=2)
        os.replace(tmp, self._path)   # atomic write
