"""Test per state_manager.py"""
import sys, os, json, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from state_manager import StateManager


@pytest.fixture
def tmp_state(tmp_path):
    return str(tmp_path / "state.json")


def test_initially_empty(tmp_state):
    sm = StateManager(tmp_state)
    assert sm.count() == 0


def test_mark_and_check(tmp_state):
    sm = StateManager(tmp_state)
    sm.mark_processed("msg_001")
    assert sm.is_processed("msg_001")
    assert not sm.is_processed("msg_002")


def test_persistence(tmp_state):
    sm1 = StateManager(tmp_state)
    sm1.mark_processed("msg_abc")
    sm1.mark_processed("msg_xyz")

    sm2 = StateManager(tmp_state)
    assert sm2.is_processed("msg_abc")
    assert sm2.is_processed("msg_xyz")
    assert sm2.count() == 2


def test_count(tmp_state):
    sm = StateManager(tmp_state)
    for i in range(5):
        sm.mark_processed(f"msg_{i}")
    assert sm.count() == 5


def test_no_duplicate_count(tmp_state):
    sm = StateManager(tmp_state)
    sm.mark_processed("same_id")
    sm.mark_processed("same_id")
    assert sm.count() == 1


def test_corrupted_state_file(tmp_state):
    """Stato corrotto → ripartenza da zero senza crash."""
    with open(tmp_state, "w") as f:
        f.write("NOT VALID JSON {{{{")
    sm = StateManager(tmp_state)
    assert sm.count() == 0
