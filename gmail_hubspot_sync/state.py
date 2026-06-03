import json
import os
from datetime import datetime, timezone


class SyncState:
    """Persists the set of already-processed Gmail message IDs and the last-run timestamp."""

    def __init__(self, state_file: str = ".processed_messages.json"):
        self._path = state_file
        self._data: dict = {"processed_ids": [], "last_run": None}
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    self._data = json.load(f)
                # Migrate list → set-backed storage
                if isinstance(self._data.get("processed_ids"), list):
                    self._data["processed_ids"] = list(
                        set(self._data["processed_ids"])
                    )
            except (json.JSONDecodeError, KeyError):
                pass

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def processed_ids(self) -> set[str]:
        return set(self._data.get("processed_ids", []))

    def mark_processed(self, message_id: str) -> None:
        ids = self.processed_ids
        ids.add(message_id)
        # Keep only the last 10 000 IDs to bound file size
        self._data["processed_ids"] = list(ids)[-10_000:]
        self._data["last_run"] = datetime.now(timezone.utc).isoformat()
        self._save()

    @property
    def last_run(self) -> datetime | None:
        raw = self._data.get("last_run")
        if raw:
            return datetime.fromisoformat(raw)
        return None

    def update_last_run(self) -> None:
        self._data["last_run"] = datetime.now(timezone.utc).isoformat()
        self._save()
