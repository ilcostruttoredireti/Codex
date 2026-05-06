"""Persistent state for tracking processed Gmail messages and last poll timestamp."""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = "sync_state.json"


class SyncState:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self._path = Path(state_file)
        self._data: dict = {"last_poll_ms": 0, "seen_message_ids": []}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                logger.debug("Loaded state from %s", self._path)
            except Exception as exc:
                logger.warning("Could not read state file, starting fresh: %s", exc)

    def _save(self) -> None:
        # Keep only the last 5000 message IDs to bound file size
        self._data["seen_message_ids"] = self._data["seen_message_ids"][-5000:]
        self._path.write_text(json.dumps(self._data, indent=2))

    @property
    def last_poll_ms(self) -> int:
        return self._data.get("last_poll_ms", 0)

    @property
    def seen_message_ids(self) -> set[str]:
        return set(self._data.get("seen_message_ids", []))

    def mark_seen(self, message_id: str) -> None:
        ids = self._data.setdefault("seen_message_ids", [])
        if message_id not in ids:
            ids.append(message_id)

    def update_poll_timestamp(self, ts_ms: int | None = None) -> None:
        self._data["last_poll_ms"] = ts_ms if ts_ms is not None else int(time.time() * 1000)
        self._save()
