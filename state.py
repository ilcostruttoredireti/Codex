import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class SyncState:
    def __init__(self, state_file: str):
        self.path = Path(state_file)
        self.last_history_id: str = ""
        self.processed_count: int = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
            self.last_history_id = data.get("last_history_id", "")
            self.processed_count = data.get("processed_count", 0)
            logger.debug(f"Stato caricato: historyId={self.last_history_id}, processate={self.processed_count}")
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Impossibile caricare lo stato da {self.path}: {e}")

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(
                    {
                        "last_history_id": self.last_history_id,
                        "processed_count": self.processed_count,
                    },
                    indent=2,
                )
            )
        except OSError as e:
            logger.error(f"Impossibile salvare lo stato: {e}")
