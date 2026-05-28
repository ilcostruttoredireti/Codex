import json
import os
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class SyncState:
    last_history_id: Optional[str] = None
    processed_message_ids: list = None

    def __post_init__(self):
        if self.processed_message_ids is None:
            self.processed_message_ids = []

    def mark_processed(self, message_id: str):
        if message_id not in self.processed_message_ids:
            self.processed_message_ids.append(message_id)
        # Keep only last 10_000 IDs to avoid unbounded growth
        if len(self.processed_message_ids) > 10_000:
            self.processed_message_ids = self.processed_message_ids[-10_000:]

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.processed_message_ids


def load_state(path: str) -> SyncState:
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        return SyncState(**data)
    return SyncState()


def save_state(state: SyncState, path: str):
    with open(path, "w") as f:
        json.dump(asdict(state), f, indent=2)
