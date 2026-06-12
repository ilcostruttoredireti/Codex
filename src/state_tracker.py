"""
Persists the set of already-processed Gmail message IDs to a local JSON file,
so the sync engine survives restarts without reprocessing old messages.
"""

import json
import os
from pathlib import Path


class StateTracker:
    def __init__(self, state_file: str = ".sync_state.json"):
        self._path = Path(state_file)
        self._seen: set[str] = self._load()

    def _load(self) -> set[str]:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                return set(data.get("processed_ids", []))
            except Exception:
                return set()
        return set()

    def _save(self) -> None:
        self._path.write_text(json.dumps({"processed_ids": sorted(self._seen)}, indent=2))

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._seen

    def mark_processed(self, message_id: str) -> None:
        self._seen.add(message_id)
        self._save()
