"""Persist sync state to avoid reprocessing emails across runs."""

import json
import os
import time


class SyncState:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_timestamp": 0, "processed_ids": []}

    def save(self):
        with open(self.state_file, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def last_timestamp(self) -> int:
        return self._data.get("last_timestamp", 0)

    def mark_processed(self, message_id: str, timestamp: int):
        processed = self._data.setdefault("processed_ids", [])
        if message_id not in processed:
            processed.append(message_id)
            # Keep only the last 5000 IDs to prevent unbounded growth
            if len(processed) > 5000:
                self._data["processed_ids"] = processed[-5000:]
        if timestamp > self._data.get("last_timestamp", 0):
            self._data["last_timestamp"] = timestamp
        self.save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data.get("processed_ids", [])

    def set_last_timestamp(self, ts: int):
        self._data["last_timestamp"] = ts
        self.save()
