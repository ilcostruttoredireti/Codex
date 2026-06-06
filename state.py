"""Gestione dello stato persistente: traccia messaggi processati e history ID."""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SyncState:
    """Stato persistente del sync Gmail → HubSpot."""

    def __init__(self, state_file: str = ".sync_state.json"):
        self.state_file = Path(state_file)
        self._data = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Stato corrotto, inizializzo nuovo: %s", e)
        return {"history_id": None, "processed_message_ids": []}

    def _save(self):
        with open(self.state_file, "w") as f:
            json.dump(self._data, f, indent=2)

    @property
    def history_id(self) -> Optional[str]:
        return self._data.get("history_id")

    @history_id.setter
    def history_id(self, value: str):
        self._data["history_id"] = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._data.get("processed_message_ids", [])

    def mark_processed(self, message_id: str, new_history_id: Optional[str] = None):
        ids = self._data.setdefault("processed_message_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # Mantieni solo gli ultimi 5000 per evitare file giganteschi
            if len(ids) > 5000:
                self._data["processed_message_ids"] = ids[-5000:]
        if new_history_id:
            self._data["history_id"] = new_history_id
        self._save()
