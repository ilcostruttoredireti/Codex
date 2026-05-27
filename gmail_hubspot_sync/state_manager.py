"""Persist the last processed Gmail historyId so we never re-process messages."""

import json
import logging
from pathlib import Path
from typing import Optional

from config import STATE_FILE

logger = logging.getLogger(__name__)
_PATH = Path(STATE_FILE)


def load_history_id() -> Optional[str]:
    if not _PATH.exists():
        return None
    try:
        data = json.loads(_PATH.read_text())
        return data.get("history_id")
    except Exception as exc:
        logger.warning("Could not read state file: %s", exc)
        return None


def save_history_id(history_id: str) -> None:
    try:
        _PATH.write_text(json.dumps({"history_id": history_id}))
    except Exception as exc:
        logger.warning("Could not write state file: %s", exc)
