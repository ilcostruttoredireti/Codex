import json
import os
import tempfile

import pytest

from gmail_hubspot_sync.state_manager import StateManager


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "state.json")


def test_new_state_file_is_empty(state_file):
    sm = StateManager(state_file)
    assert sm.last_history_id is None
    assert not sm.is_processed("abc")


def test_mark_and_check_processed(state_file):
    sm = StateManager(state_file)
    sm.mark_processed("msg1")
    assert sm.is_processed("msg1")
    assert not sm.is_processed("msg2")


def test_state_persists_across_instances(state_file):
    sm = StateManager(state_file)
    sm.mark_processed("msg99")
    sm.last_history_id = "12345"

    sm2 = StateManager(state_file)
    assert sm2.is_processed("msg99")
    assert sm2.last_history_id == "12345"


def test_no_duplicate_ids(state_file):
    sm = StateManager(state_file)
    sm.mark_processed("dup")
    sm.mark_processed("dup")
    with open(state_file) as fh:
        data = json.load(fh)
    assert data["processed_ids"].count("dup") == 1


def test_cap_at_max_stored_ids(state_file):
    sm = StateManager(state_file)
    sm._MAX_STORED_IDS = 5
    for i in range(10):
        sm.mark_processed(f"msg{i}")
    with open(state_file) as fh:
        data = json.load(fh)
    assert len(data["processed_ids"]) == 5
    assert "msg9" in data["processed_ids"]
