import json
import os
from typing import Optional

_MAX_TRACKED_IDS = 2000


class StateManager:
    def __init__(self, state_file: str):
        self.state_file = state_file
        self._state = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {'history_id': None, 'processed_ids': []}

    def _save(self):
        with open(self.state_file, 'w') as f:
            json.dump(self._state, f, indent=2)

    @property
    def history_id(self) -> Optional[str]:
        return self._state.get('history_id')

    @history_id.setter
    def history_id(self, value: str):
        self._state['history_id'] = value
        self._save()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._state.get('processed_ids', [])

    def mark_processed(self, message_id: str):
        ids = self._state.get('processed_ids', [])
        if message_id not in ids:
            ids.append(message_id)
            if len(ids) > _MAX_TRACKED_IDS:
                ids = ids[-_MAX_TRACKED_IDS:]
            self._state['processed_ids'] = ids
            self._save()
