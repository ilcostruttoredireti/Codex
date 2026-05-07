"""Persist the Gmail history ID and processed message IDs between runs."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = ".sync_state.json"


class StateManager:
    def __init__(self, state_file: str = DEFAULT_STATE_FILE):
        self.state_file = Path(state_file)
        self._state = self._load()

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Could not read state file: %s – starting fresh.", e)
        return {"history_id": None, "processed_ids": []}

    def _save(self) -> None:
        try:
            self.state_file.write_text(json.dumps(self._state, indent=2))
        except OSError as e:
            logger.error("Failed to save state: %s", e)

    # ------------------------------------------------------------------
    # History ID (Gmail delta polling)
    # ------------------------------------------------------------------

    def get_history_id(self) -> str | None:
        return self._state.get("history_id")

    def set_history_id(self, history_id: str) -> None:
        self._state["history_id"] = history_id
        self._save()

    # ------------------------------------------------------------------
    # Processed message IDs (deduplication within a session)
    # ------------------------------------------------------------------

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get("processed_ids", [])

    def mark_processed(self, message_id: str) -> None:
        ids: list = self._state.setdefault("processed_ids", [])
        if message_id not in ids:
            ids.append(message_id)
            # Keep only the last 10 000 IDs to avoid unbounded growth
            if len(ids) > 10_000:
                self._state["processed_ids"] = ids[-10_000:]
            self._save()

    def reset(self, new_history_id: str | None = None) -> None:
        """Clear state (e.g. after a history-expiry event)."""
        self._state = {"history_id": new_history_id, "processed_ids": []}
        self._save()
        logger.info("State reset. New history_id=%s", new_history_id)
