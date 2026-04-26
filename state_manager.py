import json
from pathlib import Path
from typing import Optional


class StateManager:
    """Persists Gmail historyId and processed message IDs to a JSON file."""

    def __init__(self, state_file: str):
        self._path = Path(state_file)
        self._state = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {"last_history_id": None, "processed_ids": []}

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._state, f, indent=2)

    @property
    def last_history_id(self) -> Optional[str]:
        return self._state.get("last_history_id")

    @last_history_id.setter
    def last_history_id(self, value: str) -> None:
        self._state["last_history_id"] = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids", [])

    def mark_processed(self, message_id: str) -> None:
        ids = self._state.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # Cap at 10 000 to avoid unbounded growth
            if len(ids) > 10_000:
                self._state["processed_ids"] = ids[-10_000:]
            self._save()
