import json
from pathlib import Path


class SyncState:
    """Persists Gmail history ID and processed message IDs across restarts."""

    _MAX_PROCESSED_IDS = 2000

    def __init__(self, state_file: str) -> None:
        self._path = Path(state_file)
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {"history_id": None, "processed_ids": []}

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def history_id(self) -> str | None:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._data["history_id"] = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data.get("processed_ids", [])

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._data.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # Trim to prevent unbounded growth
            self._data["processed_ids"] = ids[-self._MAX_PROCESSED_IDS :]
            self._save()
