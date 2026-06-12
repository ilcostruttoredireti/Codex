"""Persist/restore SyncState to a JSON file between runs."""

import json
import logging
from pathlib import Path

from .sync_engine import SyncState

log = logging.getLogger(__name__)
_DEFAULT_PATH = Path(".sync_state.json")


def load(path: Path = _DEFAULT_PATH) -> SyncState:
    if not path.exists():
        return SyncState()
    try:
        data = json.loads(path.read_text())
        return SyncState(
            processed_ids=set(data.get("processed_ids", [])),
            last_timestamp=int(data.get("last_timestamp", 0)),
            gmail_label_id=data.get("gmail_label_id"),
        )
    except Exception as exc:
        log.warning("Stato non leggibile (%s), parto da zero.", exc)
        return SyncState()


def save(state: SyncState, path: Path = _DEFAULT_PATH) -> None:
    data = {
        "processed_ids": list(state.processed_ids),
        "last_timestamp": state.last_timestamp,
        "gmail_label_id": state.gmail_label_id,
    }
    path.write_text(json.dumps(data, indent=2))
    log.debug("Stato salvato in %s", path)
