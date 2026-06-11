"""Persistent state: last-check timestamp and processed message IDs."""

import json
import logging
import os
import time
from typing import List

logger = logging.getLogger(__name__)

_MAX_STORED_IDS = 10_000
_TRIM_TO = 5_000


class StateManager:
    """Reads and writes sync state to a local JSON file."""

    def __init__(self, state_file: str):
        self._path = state_file
        self._state = self._load()

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path, "r") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(f"Cannot read state file '{self._path}': {exc}. Starting fresh.")
        # Default: look back 24 hours on first run
        return {
            "last_check_unix": int(time.time()) - 86_400,
            "processed_ids": [],
        }

    def _save(self) -> None:
        try:
            with open(self._path, "w") as fh:
                json.dump(self._state, fh, indent=2)
        except OSError as exc:
            logger.error(f"Cannot write state file: {exc}")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    @property
    def last_check_unix(self) -> int:
        return int(self._state.get("last_check_unix", 0))

    def update_last_check(self, ts: int = 0) -> None:
        self._state["last_check_unix"] = ts or int(time.time())
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids", [])

    def mark_processed(self, message_id: str) -> None:
        ids: List[str] = self._state.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # Prevent unbounded growth
            if len(ids) > _MAX_STORED_IDS:
                self._state["processed_ids"] = ids[-_TRIM_TO:]
        self._save()
