import json
import os
from typing import Set


class StateManager:
    """Persists the set of already-processed Gmail message IDs to disk."""

    def __init__(self, path: str):
        self._path = path
        self._processed: Set[str] = self._load()

    def _load(self) -> Set[str]:
        if not os.path.exists(self._path):
            return set()
        with open(self._path) as f:
            data = json.load(f)
        return set(data.get("processed_ids", []))

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump({"processed_ids": list(self._processed)}, f, indent=2)

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed

    def mark_processed(self, message_id: str) -> None:
        self._processed.add(message_id)
        self._save()
