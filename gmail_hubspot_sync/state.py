import json
import logging
import os

logger = logging.getLogger(__name__)

_MAX_IDS = 10_000   # cap processed-IDs list to avoid unbounded growth


class SyncState:
    """Persist Gmail historyId and the set of already-processed message IDs."""

    def __init__(self, path: str = "sync_state.json"):
        self._path = path
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self._path):
            try:
                with open(self._path) as f:
                    return json.load(f)
            except Exception as exc:
                logger.warning(f"Impossibile leggere lo state file: {exc}. Parto da zero.")
        return {"history_id": None, "processed_ids": []}

    def _save(self) -> None:
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    # ── history ID ────────────────────────────────────────────────────────────

    @property
    def history_id(self) -> str | None:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str) -> None:
        self._data["history_id"] = value
        self._save()

    # ── processed message IDs ─────────────────────────────────────────────────

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self._data.get("processed_ids", [])

    def mark_processed(self, msg_id: str) -> None:
        ids: list = self._data.setdefault("processed_ids", [])
        if msg_id not in ids:
            ids.append(msg_id)
            if len(ids) > _MAX_IDS:
                self._data["processed_ids"] = ids[-_MAX_IDS:]
            self._save()
