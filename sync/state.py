"""Persistent state: last Gmail historyId and set of processed message IDs."""
import json
import logging
import os
from typing import Optional, Set

logger = logging.getLogger(__name__)

_MAX_PROCESSED_IDS = 5000  # keep memory bounded


class SyncState:
    def __init__(self, path: str):
        self._path = path
        self._history_id: Optional[str] = None
        self._processed_ids: Set[str] = set()
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def history_id(self) -> Optional[str]:
        return self._history_id

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._history_id = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed_ids

    def mark_processed(self, message_id: str) -> None:
        self._processed_ids.add(message_id)
        if len(self._processed_ids) > _MAX_PROCESSED_IDS:
            # Drop the oldest half (sets are unordered; just trim arbitrarily)
            excess = len(self._processed_ids) - _MAX_PROCESSED_IDS // 2
            self._processed_ids = set(list(self._processed_ids)[excess:])
        self._save()

    def initialize(self, history_id: str) -> None:
        """Called on first run to set the starting historyId."""
        self._history_id = history_id
        self._processed_ids = set()
        self._save()
        logger.info(f"State initialized with historyId={history_id}")

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._history_id = data.get("history_id")
            self._processed_ids = set(data.get("processed_ids", []))
            logger.debug(f"Loaded state: historyId={self._history_id}, "
                         f"{len(self._processed_ids)} processed IDs")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load state file '{self._path}': {e}. Starting fresh.")

    def _save(self) -> None:
        try:
            with open(self._path, "w") as f:
                json.dump(
                    {"history_id": self._history_id,
                     "processed_ids": list(self._processed_ids)},
                    f,
                    indent=2,
                )
        except OSError as e:
            logger.error(f"Could not save state file: {e}")
