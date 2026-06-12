import json
import os
from typing import Optional


class StateManager:
    """
    Mantiene lo stato di sincronizzazione su disco.
    - last_history_id: usato dall'API Gmail History per sync incrementale
    - processed_message_ids: set degli ID messaggi già processati (anti-duplicati)
    """

    _MAX_IDS = 10_000   # soglia prima del trimming
    _TRIM_TO = 5_000    # quanti ID conservare dopo il trim

    def __init__(self, state_file: str):
        self._file = state_file
        self._history_id: Optional[str] = None
        self._processed: set[str] = set()
        self._load()

    # ── Persistenza ──────────────────────────────────────────────────────────

    def _load(self):
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r") as f:
                data = json.load(f)
            self._history_id = data.get("last_history_id")
            self._processed = set(data.get("processed_message_ids", []))
        except (json.JSONDecodeError, OSError):
            pass

    def save(self):
        data = {
            "last_history_id": self._history_id,
            "processed_message_ids": list(self._processed),
        }
        with open(self._file, "w") as f:
            json.dump(data, f, indent=2)

    # ── API pubblica ──────────────────────────────────────────────────────────

    @property
    def last_history_id(self) -> Optional[str]:
        return self._history_id

    @last_history_id.setter
    def last_history_id(self, value: str):
        self._history_id = value

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed

    def mark_processed(self, message_id: str):
        self._processed.add(message_id)
        if len(self._processed) > self._MAX_IDS:
            # Mantieni solo gli ultimi N id (ordine non garantito: set → list → trim)
            self._processed = set(list(self._processed)[-self._TRIM_TO:])
