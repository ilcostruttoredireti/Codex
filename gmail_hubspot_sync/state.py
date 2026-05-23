"""Persist the last-seen Gmail historyId between runs."""

import json
import os

from config import Config


def load_history_id() -> str | None:
    if not os.path.exists(Config.STATE_FILE):
        return None
    try:
        with open(Config.STATE_FILE) as f:
            return json.load(f).get("history_id")
    except (json.JSONDecodeError, KeyError):
        return None


def save_history_id(history_id: str) -> None:
    with open(Config.STATE_FILE, "w") as f:
        json.dump({"history_id": history_id}, f)
