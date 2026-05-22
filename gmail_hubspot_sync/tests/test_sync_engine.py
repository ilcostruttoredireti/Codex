import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from config import Config
from gmail_client import GmailMessage
from hubspot_client import HubSpotContact
from sync_engine import SyncEngine, SyncStatus
from state_manager import StateManager


def _make_message(sender: str, msg_id: str = "msg1") -> GmailMessage:
    return GmailMessage(
        message_id=msg_id,
        thread_id="thread1",
        sender=sender,
        subject="Test subject",
        date=datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc),
        snippet="Test snippet",
    )


def _engine(gmail_msgs=None, existing_contact=None, create_contact=None):
    config = Config()
    config.HUBSPOT_API_KEY = "test-key"
    config.STATE_FILE = "/tmp/test_sync_state.json"

    gmail = MagicMock()
    gmail.get_new_messages.return_value = gmail_msgs or []

    hubspot = MagicMock()
    hubspot.find_contact_by_email.return_value = existing_contact
    hubspot.create_contact.return_value = create_contact or HubSpotContact(
        id="NEW123", email="new@example.com"
    )
    hubspot.log_email_activity.return_value = "note1"

    state = MagicMock(spec=StateManager)
    state.get_last_sync.return_value = None
    state.is_processed.return_value = False

    return SyncEngine(config, gmail, hubspot, state), hubspot


def test_creates_new_contact():
    msg = _make_message("Mario Rossi <mario@example.com>")
    engine, hs = _engine(gmail_msgs=[msg])

    results = engine.run_once()

    assert len(results) == 1
    assert results[0].status == SyncStatus.CREATED
    assert results[0].email == "mario@example.com"
    hs.create_contact.assert_called_once()
    hs.log_email_activity.assert_called_once()


def test_updates_existing_contact():
    existing = HubSpotContact(id="EX1", email="mario@example.com", first_name="", last_name="", company="")
    msg = _make_message("Mario Rossi <mario@example.com>")
    engine, hs = _engine(gmail_msgs=[msg], existing_contact=existing)

    results = engine.run_once()

    assert results[0].status == SyncStatus.UPDATED
    assert results[0].hubspot_id == "EX1"
    hs.update_contact.assert_called_once()


def test_ignores_already_complete_contact():
    existing = HubSpotContact(
        id="EX2", email="mario@example.com",
        first_name="Mario", last_name="Rossi", company="Example"
    )
    msg = _make_message("Mario Rossi <mario@example.com>")
    engine, hs = _engine(gmail_msgs=[msg], existing_contact=existing)

    results = engine.run_once()

    assert results[0].status == SyncStatus.UPDATED
    hs.update_contact.assert_not_called()  # no missing fields → no update call


def test_ignores_empty_sender():
    msg = _make_message("")
    engine, hs = _engine(gmail_msgs=[msg])
    results = engine.run_once()
    assert results[0].status == SyncStatus.IGNORED
    hs.create_contact.assert_not_called()


def test_skips_already_processed():
    msg = _make_message("mario@example.com")
    engine, hs = _engine(gmail_msgs=[msg])
    engine._state.is_processed.return_value = True

    results = engine.run_once()

    assert results == []
    hs.create_contact.assert_not_called()
