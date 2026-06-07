import json
import os
from datetime import datetime, timezone


class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            with open(self.state_file, "r") as f:
                return json.load(f)
        return {"processed_ids": [], "last_check_timestamp": None}

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump(self._state, f, indent=2)

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state["processed_ids"]

    def mark_processed(self, message_id: str):
        if message_id not in self._state["processed_ids"]:
            self._state["processed_ids"].append(message_id)
            # Keep only the last 10000 IDs to avoid unbounded growth
            if len(self._state["processed_ids"]) > 10000:
                self._state["processed_ids"] = self._state["processed_ids"][-10000:]
        self._save()

    def get_last_check(self) -> str | None:
        return self._state.get("last_check_timestamp")

    def update_last_check(self):
        self._state["last_check_timestamp"] = datetime.now(timezone.utc).isoformat()
        self._save()
