"""Persists the Gmail history ID so each run only processes new messages."""

import json
import os
from typing import Optional


class SyncState:
    def __init__(self, path: str):
        self._path = path
        self._data: dict = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f)

    @property
    def history_id(self) -> Optional[str]:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._data["history_id"] = value
        self._save()

    def mark_processed(self, message_id: str) -> None:
        processed = self._data.setdefault("processed_ids", [])
        if message_id not in processed:
            processed.append(message_id)
            # Keep only the last 10 000 IDs to avoid unbounded growth
            self._data["processed_ids"] = processed[-10_000:]
            self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data.get("processed_ids", [])
