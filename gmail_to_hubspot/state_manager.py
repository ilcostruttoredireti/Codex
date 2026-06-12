import json
from pathlib import Path
from typing import Optional

_STATE_FILE = Path("gmail_hubspot_state.json")
_MAX_IDS = 50_000


def load() -> dict:
    if _STATE_FILE.exists():
        with _STATE_FILE.open() as f:
            return json.load(f)
    return {"processed": [], "history_id": None}


def save(state: dict) -> None:
    with _STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)


def is_processed(state: dict, message_id: str) -> bool:
    return message_id in state.get("processed", [])


def mark_processed(state: dict, message_id: str) -> None:
    processed = state.setdefault("processed", [])
    processed.append(message_id)
    if len(processed) > _MAX_IDS:
        state["processed"] = processed[-_MAX_IDS:]
