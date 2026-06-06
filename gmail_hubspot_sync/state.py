"""Persistent state — tracks processed Gmail message IDs and the current historyId."""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_PATH = "sync_state.json"


class SyncState:
    """Thin JSON-backed store for deduplication and history tracking."""

    def __init__(self, path: str = DEFAULT_PATH):
        self._path = path
        self._data = {"processed_ids": [], "history_id": None}
        self._load()

    def _load(self):
        if os.path.exists(self._path):
            try:
                with open(self._path) as fh:
                    self._data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                logger.warning("State file corrupt or unreadable — starting fresh.")

    def save(self):
        try:
            with open(self._path, "w") as fh:
                json.dump(self._data, fh, indent=2)
        except OSError:
            logger.exception("Could not write state file.")

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self._data["processed_ids"]

    def mark_processed(self, msg_id: str):
        ids = self._data["processed_ids"]
        if msg_id not in ids:
            ids.append(msg_id)
        # Keep the list bounded — only retain the last 10 000 IDs
        if len(ids) > 10_000:
            self._data["processed_ids"] = ids[-10_000:]

    @property
    def history_id(self) -> Optional[str]:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str):
        self._data["history_id"] = value
