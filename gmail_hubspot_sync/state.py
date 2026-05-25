"""Persistent state tracking — remembers which Gmail messages were processed."""

import json
import logging
from pathlib import Path
from typing import Set

logger = logging.getLogger(__name__)


class ProcessedMessageTracker:
    """
    Lightweight JSON-backed set of processed Gmail message IDs.

    Prevents re-processing the same email across restarts.
    """

    def __init__(self, state_file: str):
        self._path = Path(state_file)
        self._ids: Set[str] = set()
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._ids = set(data.get("processed_ids", []))
                logger.info(
                    "State caricato: %d ID già processati", len(self._ids)
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Impossibile leggere state file: %s — parto da zero", exc)
                self._ids = set()
        else:
            logger.info("State file non trovato — partenza a freddo")

    def _save(self):
        self._path.write_text(
            json.dumps({"processed_ids": list(self._ids)}, indent=2),
            encoding="utf-8",
        )

    def __contains__(self, msg_id: str) -> bool:
        return msg_id in self._ids

    def mark_processed(self, msg_id: str):
        """Mark a message ID as processed and persist."""
        self._ids.add(msg_id)
        self._save()

    @property
    def ids(self) -> Set[str]:
        return frozenset(self._ids)

    def __len__(self) -> int:
        return len(self._ids)
