import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional, Set

logger = logging.getLogger(__name__)


class StateManager:
    """Persists sync state: last-run timestamp and processed message IDs."""

    def __init__(self, state_file: str):
        self.state_file = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not read state file: %s — starting fresh", e)
        return {"last_timestamp": None, "processed_ids": []}

    def _save(self) -> None:
        try:
            with open(self.state_file, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            logger.error("Could not save state: %s", e)

    @property
    def last_timestamp(self) -> Optional[datetime]:
        ts = self._state.get("last_timestamp")
        if ts:
            return datetime.fromisoformat(ts)
        return None

    def set_last_timestamp(self, dt: datetime) -> None:
        self._state["last_timestamp"] = dt.isoformat()
        self._save()

    @property
    def processed_ids(self) -> Set[str]:
        return set(self._state.get("processed_ids", []))

    def mark_processed(self, message_id: str) -> None:
        ids = self.processed_ids
        ids.add(message_id)
        # Keep only the latest 5000 IDs to bound file size
        self._state["processed_ids"] = list(ids)[-5000:]
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.processed_ids
