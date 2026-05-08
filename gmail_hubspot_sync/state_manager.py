"""
Persists the sync state to a JSON file so the process can resume
after a restart without reprocessing old messages.

State schema:
{
  "last_history_id": "12345",          # Gmail historyId from the previous run
  "processed_ids": ["id1", "id2", …]   # Gmail message IDs already synced
}
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "sync_state.json"
# Maximum number of processed IDs to retain (circular buffer)
_MAX_PROCESSED = 5_000


class StateManager:
    def __init__(self, path: str = _DEFAULT_PATH):
        self._path = path
        self._state = self._load()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def last_history_id(self) -> Optional[str]:
        return self._state.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str) -> None:
        self._state["last_history_id"] = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids_set", set())

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._state.setdefault("processed_ids", [])
        ids_set: set = self._state.setdefault("processed_ids_set", set())

        if message_id in ids_set:
            return

        ids.append(message_id)
        ids_set.add(message_id)

        # Trim oldest entries to cap memory usage
        if len(ids) > _MAX_PROCESSED:
            removed = ids[: len(ids) - _MAX_PROCESSED]
            self._state["processed_ids"] = ids[-_MAX_PROCESSED:]
            for r in removed:
                ids_set.discard(r)

        self._save()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if not os.path.exists(self._path):
            return {}
        try:
            with open(self._path) as f:
                data = json.load(f)
            # Rebuild the fast-lookup set (not stored in JSON)
            data["processed_ids_set"] = set(data.get("processed_ids", []))
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read state file %s: %s. Starting fresh.", self._path, exc)
            return {}

    def _save(self) -> None:
        # Never persist the ephemeral set
        serialisable = {k: v for k, v in self._state.items() if k != "processed_ids_set"}
        try:
            tmp_path = self._path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(serialisable, f, indent=2)
            os.replace(tmp_path, self._path)
        except OSError as exc:
            logger.error("Failed to save state: %s", exc)
