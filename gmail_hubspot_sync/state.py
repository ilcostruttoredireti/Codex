from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SyncState:
    def __init__(self, state_file: str):
        self.path = Path(state_file)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Could not load state file: %s", exc)
        return {"history_id": None, "processed_ids": []}

    def save(self) -> None:
        try:
            self.path.write_text(json.dumps(self._data, indent=2))
        except OSError as exc:
            logger.error("Could not save state file: %s", exc)

    @property
    def history_id(self) -> Optional[str]:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._data["history_id"] = value

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data.get("processed_ids", [])

    def mark_processed(self, message_id: str, max_cache: int = 10000) -> None:
        ids: list = self._data.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
        if len(ids) > max_cache:
            self._data["processed_ids"] = ids[-max_cache:]
