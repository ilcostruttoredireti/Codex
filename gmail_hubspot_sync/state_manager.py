"""Persist sync state (last run timestamp, processed message IDs) to disk."""

import json
import os
import time
from typing import Set


class StateManager:
    def __init__(self, path: str = "sync_state.json"):
        self.path = path
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path) as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_run_epoch": 0, "processed_ids": []}

    def save(self) -> None:
        with open(self.path, "w") as fh:
            json.dump(self._state, fh, indent=2)

    @property
    def last_run_epoch(self) -> int:
        return int(self._state.get("last_run_epoch", 0))

    def update_last_run(self) -> None:
        self._state["last_run_epoch"] = int(time.time())
        self.save()

    @property
    def processed_ids(self) -> Set[str]:
        return set(self._state.get("processed_ids", []))

    def mark_processed(self, msg_id: str) -> None:
        ids = self.processed_ids
        ids.add(msg_id)
        # Keep the last 10 000 IDs to bound memory usage
        self._state["processed_ids"] = list(ids)[-10_000:]
        self.save()

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self.processed_ids
