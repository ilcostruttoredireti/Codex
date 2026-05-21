"""
Persiste l'ultimo historyId di Gmail processato su disco
per riprendere il monitoraggio dopo un riavvio.
"""

import json
from pathlib import Path
from typing import Optional

import config


def load_history_id() -> Optional[str]:
    path = Path(config.STATE_FILE)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            return data.get("history_id")
        except (json.JSONDecodeError, IOError):
            pass
    return None


def save_history_id(history_id: str) -> None:
    path = Path(config.STATE_FILE)
    path.write_text(json.dumps({"history_id": history_id}))
