import json
import os


class ProcessedStore:
    """Persistent set of already-processed Gmail message IDs."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if os.path.exists(self._path):
            with open(self._path) as f:
                self._ids = set(json.load(f))

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(list(self._ids), f)

    def seen(self, msg_id: str) -> bool:
        return msg_id in self._ids

    def mark(self, msg_id: str) -> None:
        self._ids.add(msg_id)
        self._save()

    def __len__(self) -> int:
        return len(self._ids)
