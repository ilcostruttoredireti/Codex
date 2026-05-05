import json
import os

_STATE_FILE = os.getenv('STATE_FILE', 'sync_state.json')


def _load() -> dict:
    if os.path.exists(_STATE_FILE):
        with open(_STATE_FILE) as fh:
            return json.load(fh)
    return {}


def _save(data: dict):
    with open(_STATE_FILE, 'w') as fh:
        json.dump(data, fh, indent=2)


def get_history_id() -> str | None:
    return _load().get('history_id')


def set_history_id(history_id: str):
    data = _load()
    data['history_id'] = history_id
    _save(data)
