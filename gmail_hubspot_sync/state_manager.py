"""
Gestisce lo stato persistente su file JSON.
Traccia gli ID dei messaggi Gmail già processati.
"""
import json
import logging
import os
from typing import Set

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._processed_ids: Set[str] = set()
        self._load()

    # ── Private ──────────────────────────────────────────────────────────────

    def _load(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self._processed_ids = set(data.get("processed_message_ids", []))
                logger.info(
                    "Stato caricato: %d messaggi già processati",
                    len(self._processed_ids),
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning("Impossibile leggere lo stato: %s — ripartenza da zero", e)
                self._processed_ids = set()
        else:
            logger.info("Nessun file di stato trovato — avvio da zero.")

    def _save(self):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"processed_message_ids": sorted(self._processed_ids)},
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except IOError as e:
            logger.error("Impossibile salvare lo stato: %s", e)

    # ── Public ───────────────────────────────────────────────────────────────

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._processed_ids

    def mark_processed(self, message_id: str):
        self._processed_ids.add(message_id)
        self._save()

    def count(self) -> int:
        return len(self._processed_ids)
