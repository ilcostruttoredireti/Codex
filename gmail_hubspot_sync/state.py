import json
import os
from typing import Optional

from . import config


def _path() -> str:
    return config.STATE_FILE


def load() -> dict:
    if os.path.exists(_path()):
        with open(_path()) as fh:
            return json.load(fh)
    return {"history_id": None, "processed_message_ids": []}


def save(state: dict) -> None:
    with open(_path(), "w") as fh:
        json.dump(state, fh, indent=2)


def get_history_id() -> Optional[str]:
    return load().get("history_id")


def set_history_id(history_id: str) -> None:
    state = load()
    state["history_id"] = history_id
    save(state)


def is_processed(message_id: str) -> bool:
    return message_id in load().get("processed_message_ids", [])


def mark_processed(message_id: str) -> None:
    state = load()
    ids: list = state.setdefault("processed_message_ids", [])
    if message_id not in ids:
        ids.append(message_id)
    # Keep only the last 5000 to limit file growth
    state["processed_message_ids"] = ids[-5000:]
    save(state)
