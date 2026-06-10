"""Persist the Gmail history cursor between sync cycles."""

import json
from pathlib import Path
from typing import Optional

_STATE_FILE = Path(__file__).parent / ".sync_state.json"


def _load() -> dict:
    if _STATE_FILE.exists():
        with open(_STATE_FILE) as f:
            return json.load(f)
    return {}


def _save(state: dict) -> None:
    with open(_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_history_id() -> Optional[str]:
    """Return the Gmail historyId saved from the previous cycle, or None on first run."""
    return _load().get("history_id")


def set_history_id(history_id: str) -> None:
    state = _load()
    state["history_id"] = str(history_id)
    _save(state)
