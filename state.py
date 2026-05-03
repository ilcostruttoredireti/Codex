import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MAX_IDS = 2000


class StateManager:
    def __init__(self, state_file: str):
        self._path = Path(state_file)
        self._state = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Could not load state file, starting fresh: {e}")
        return {"history_id": None, "processed_ids": []}

    def save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._state, f, indent=2)

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state["processed_ids"]

    def mark_processed(self, message_id: str) -> None:
        ids = self._state["processed_ids"]
        if message_id not in ids:
            ids.append(message_id)
            # Prevent unbounded growth
            self._state["processed_ids"] = ids[-_MAX_IDS:]

    def get_history_id(self) -> str | None:
        return self._state.get("history_id")

    def set_history_id(self, history_id: str) -> None:
        self._state["history_id"] = str(history_id)
