import json
import os
from dataclasses import dataclass, asdict, field
from typing import List, Optional


@dataclass
class SyncState:
    last_history_id: Optional[str] = None
    processed_message_ids: List[str] = field(default_factory=list)


def load_state(state_file: str) -> SyncState:
    if os.path.exists(state_file):
        with open(state_file) as f:
            data = json.load(f)
        return SyncState(
            last_history_id=data.get("last_history_id"),
            processed_message_ids=data.get("processed_message_ids", []),
        )
    return SyncState()


def save_state(state: SyncState, state_file: str) -> None:
    with open(state_file, "w") as f:
        json.dump(asdict(state), f, indent=2)
