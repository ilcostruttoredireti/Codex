"""
Persists sync state (last run timestamp + processed message IDs) to a JSON file.
Prevents duplicate processing across restarts.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_file: str):
        self._path = Path(state_file)
        self._state: dict = self._load()

    # ------------------------------------------------------------------

    def get_last_sync(self) -> datetime | None:
        ts = self._state.get("last_sync_utc")
        if ts:
            return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        return None

    def set_last_sync(self, dt: datetime) -> None:
        self._state["last_sync_utc"] = dt.isoformat()
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids", set())

    def mark_processed(self, message_id: str) -> None:
        processed = self._state.setdefault("processed_ids", [])
        if message_id not in processed:
            processed.append(message_id)
            # Keep only the last 10 000 IDs to bound file size
            self._state["processed_ids"] = processed[-10_000:]
            self._save()

    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning("Could not load state file %s: %s — starting fresh", self._path, exc)
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(self._state, f, indent=2)
