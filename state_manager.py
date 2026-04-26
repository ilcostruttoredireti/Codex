"""
Persist sync state (Gmail history ID, processed message IDs) across runs.
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_STATE = {
    "history_id": None,
    "processed_ids": [],
}

# Keep at most this many processed IDs in memory to bound file size
_MAX_PROCESSED_IDS = 5000


class StateManager:
    def __init__(self, state_file: str):
        self._path = Path(state_file)
        self._state = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                # Migrate old formats
                if "processed_ids" not in data:
                    data["processed_ids"] = []
                return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not read state file %s: %s — starting fresh.", self._path, e)
        return dict(_DEFAULT_STATE)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._state, indent=2))

    # ------------------------------------------------------------------
    # History ID
    # ------------------------------------------------------------------

    @property
    def history_id(self) -> Optional[str]:
        return self._state.get("history_id")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._state["history_id"] = value
        self._save()

    # ------------------------------------------------------------------
    # Processed message IDs (deduplication)
    # ------------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state["processed_ids"]

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._state["processed_ids"]
        if message_id not in ids:
            ids.append(message_id)
            # Trim to avoid unbounded growth
            if len(ids) > _MAX_PROCESSED_IDS:
                self._state["processed_ids"] = ids[-_MAX_PROCESSED_IDS:]
            self._save()
