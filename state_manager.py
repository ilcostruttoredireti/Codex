"""Persist sync state between runs (processed message IDs + last epoch)."""

import json
import logging
import os
from typing import Set

logger = logging.getLogger(__name__)

_MAX_IDS = 20_000


class StateManager:
    def __init__(self, state_file: str = "sync_state.json"):
        self.state_file = state_file
        self.processed_ids: Set[str] = set()
        self.last_epoch: int = 0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.state_file):
            return
        try:
            with open(self.state_file) as fh:
                data = json.load(fh)
            self.processed_ids = set(data.get("processed_ids", []))
            self.last_epoch = int(data.get("last_epoch", 0))
        except Exception as exc:
            logger.warning("Cannot load state file %s: %s", self.state_file, exc)

    def save(self) -> None:
        # Trim oldest IDs when the set grows too large
        if len(self.processed_ids) > _MAX_IDS:
            self.processed_ids = set(sorted(self.processed_ids)[-_MAX_IDS:])
        try:
            with open(self.state_file, "w") as fh:
                json.dump(
                    {"processed_ids": list(self.processed_ids), "last_epoch": self.last_epoch},
                    fh,
                    indent=2,
                )
        except Exception as exc:
            logger.error("Cannot save state: %s", exc)

    def is_processed(self, msg_id: str) -> bool:
        return msg_id in self.processed_ids

    def mark_processed(self, msg_id: str) -> None:
        self.processed_ids.add(msg_id)

    def update_epoch(self, epoch: int) -> None:
        if epoch > self.last_epoch:
            self.last_epoch = epoch
