import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_STATE_FILE = Path("sync_state.json")


class SyncState:
    def __init__(self, state_file: Path = DEFAULT_STATE_FILE):
        self.state_file = Path(state_file)
        self.history_id: Optional[str] = None
        self.processed_ids: set[str] = set()
        self._load()

    def _load(self):
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            self.history_id = data.get("history_id")
            self.processed_ids = set(data.get("processed_ids", []))
            logger.debug(
                f"Loaded state: history_id={self.history_id}, "
                f"{len(self.processed_ids)} processed IDs"
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not load state file: {e}, starting fresh")

    def save(self):
        data = {
            "history_id": self.history_id,
            "processed_ids": list(self.processed_ids),
            "last_updated": datetime.now().isoformat(),
        }
        self.state_file.write_text(json.dumps(data, indent=2))

    def update_history_id(self, history_id: str):
        self.history_id = history_id
        self.save()

    def mark_processed(self, message_id: str):
        self.processed_ids.add(message_id)
        # Keep memory bounded — drop oldest half when over 10k
        if len(self.processed_ids) > 10_000:
            self.processed_ids = set(list(self.processed_ids)[-5_000:])
        self.save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.processed_ids
