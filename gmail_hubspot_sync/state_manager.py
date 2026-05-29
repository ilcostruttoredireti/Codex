"""Persist sync state between runs to avoid reprocessing messages."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class StateManager:
    # Maximum number of processed IDs to keep (prevents unbounded growth)
    _MAX_IDS = 2000

    def __init__(self, state_file: str = "state.json") -> None:
        self._path = Path(state_file)
        self._state = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_checked_at": None, "processed_ids": []}

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Trim old IDs before persisting
        ids = self._state.get("processed_ids", [])
        self._state["processed_ids"] = ids[-self._MAX_IDS :]
        with open(self._path, "w") as f:
            json.dump(self._state, f, indent=2)

    @property
    def last_checked_at(self) -> Optional[datetime]:
        ts = self._state.get("last_checked_at")
        if ts:
            try:
                return datetime.fromisoformat(ts)
            except ValueError:
                pass
        return None

    def mark_checked_now(self) -> None:
        self._state["last_checked_at"] = datetime.now(timezone.utc).isoformat()
        self.save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids", [])

    def mark_processed(self, message_id: str) -> None:
        ids = self._state.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
