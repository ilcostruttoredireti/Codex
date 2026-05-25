"""Tests for ProcessedMessageTracker."""

import json
import tempfile
from pathlib import Path

import pytest

from gmail_hubspot_sync.state import ProcessedMessageTracker


@pytest.fixture
def tmp_state(tmp_path):
    return str(tmp_path / "state.json")


class TestProcessedMessageTracker:
    def test_empty_on_new_file(self, tmp_state):
        tracker = ProcessedMessageTracker(tmp_state)
        assert len(tracker) == 0

    def test_mark_and_contains(self, tmp_state):
        tracker = ProcessedMessageTracker(tmp_state)
        tracker.mark_processed("abc123")
        assert "abc123" in tracker
        assert "xyz" not in tracker

    def test_persists_across_instances(self, tmp_state):
        t1 = ProcessedMessageTracker(tmp_state)
        t1.mark_processed("msg1")
        t1.mark_processed("msg2")

        t2 = ProcessedMessageTracker(tmp_state)
        assert "msg1" in t2
        assert "msg2" in t2

    def test_no_duplicate_ids(self, tmp_state):
        tracker = ProcessedMessageTracker(tmp_state)
        tracker.mark_processed("dup")
        tracker.mark_processed("dup")
        assert len(tracker) == 1

    def test_corrupted_state_file_graceful(self, tmp_path):
        state_path = tmp_path / "state.json"
        state_path.write_text("NOT VALID JSON", encoding="utf-8")
        tracker = ProcessedMessageTracker(str(state_path))
        assert len(tracker) == 0  # starts fresh

    def test_ids_property_frozen(self, tmp_state):
        tracker = ProcessedMessageTracker(tmp_state)
        tracker.mark_processed("x")
        ids = tracker.ids
        assert "x" in ids
        # frozenset — cannot mutate
        with pytest.raises(AttributeError):
            ids.add("y")  # type: ignore
