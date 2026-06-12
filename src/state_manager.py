import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger(__name__)

_MAX_IDS = 10_000


class StateManager:
    def __init__(self, state_file: str):
        self._file = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._file):
            try:
                with open(self._file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as exc:
                logger.warning("State file unreadable (%s). Starting fresh.", exc)
        return {"last_timestamp": None, "processed_ids": []}

    def get_last_timestamp(self) -> Optional[int]:
        return self._state.get("last_timestamp")

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self._state.get("processed_ids", [])

    def mark_processed(self, msg_id: str) -> None:
        ids: list = self._state.setdefault("processed_ids", [])
        if msg_id not in ids:
            ids.append(msg_id)
            if len(ids) > _MAX_IDS:
                self._state["processed_ids"] = ids[-_MAX_IDS:]

    def advance_timestamp(self) -> None:
        # Subtract 60 s to allow for slight clock drift / delivery lag
        self._state["last_timestamp"] = int(time.time()) - 60

    def save(self) -> None:
        parent = os.path.dirname(self._file)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self._file, "w") as f:
            json.dump(self._state, f, indent=2)
