import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import gmail_hubspot_sync.state as state_module
import gmail_hubspot_sync.config as config


def test_state_roundtrip(tmp_path):
    config.STATE_FILE = str(tmp_path / "state.json")
    state_module.set_history_id("abc123")
    assert state_module.get_history_id() == "abc123"


def test_mark_and_check_processed(tmp_path):
    config.STATE_FILE = str(tmp_path / "state.json")
    assert state_module.is_processed("msg_1") is False
    state_module.mark_processed("msg_1")
    assert state_module.is_processed("msg_1") is True


def test_dedup_limit(tmp_path):
    config.STATE_FILE = str(tmp_path / "state.json")
    for i in range(5100):
        state_module.mark_processed(f"msg_{i}")
    data = json.loads(open(config.STATE_FILE).read())
    assert len(data["processed_message_ids"]) == 5000
