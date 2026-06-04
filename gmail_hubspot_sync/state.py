import json
import os
from typing import Set


class SyncState:
    """
    Persists the set of Gmail message IDs that have already been processed,
    preventing duplicate HubSpot writes across restarts.
    """

    _MAX_IDS = 50_000  # cap to keep the state file manageable

    def __init__(self, path: str):
        self._path = path
        self._ids: Set[str] = set()
        self._load()

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._ids = set(data.get("processed", []))
        except (json.JSONDecodeError, IOError):
            self._ids = set()

    def save(self):
        ids_list = list(self._ids)[-self._MAX_IDS:]
        with open(self._path, "w") as f:
            json.dump({"processed": ids_list}, f, indent=None)

    def seen(self, message_id: str) -> bool:
        return message_id in self._ids

    def mark(self, message_id: str):
        self._ids.add(message_id)
