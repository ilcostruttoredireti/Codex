import sys
from unittest.mock import MagicMock

# Stub out google-auth before it gets imported so tests run without
# the native cryptography build that is broken in this environment.
for _mod in [
    "google",
    "google.auth",
    "google.auth.transport",
    "google.auth.transport.requests",
    "google.oauth2",
    "google.oauth2.credentials",
    "google_auth_oauthlib",
    "google_auth_oauthlib.flow",
    "googleapiclient",
    "googleapiclient.discovery",
]:
    sys.modules.setdefault(_mod, MagicMock())

import pytest  # noqa: E402

from gmail_hubspot_sync.sync_engine import (  # noqa: E402
    SyncEngine,
    STATUS_CREATED,
    STATUS_UPDATED,
    STATUS_SKIPPED,
    STATUS_ERROR,
)


def _make_engine(gmail=None, hubspot=None, state=None):
    gmail = gmail or MagicMock()
    hubspot = hubspot or MagicMock()
    state = state or MagicMock(
        last_history_id=None, is_processed=MagicMock(return_value=False)
    )
    return SyncEngine(gmail, hubspot, state)


def _metadata(from_val: str, subject: str = "Test") -> dict:
    return {
        "payload": {
            "headers": [
                {"name": "From", "value": from_val},
                {"name": "Subject", "value": subject},
            ]
        }
    }


class TestRunOnce:
    def test_creates_new_contact(self):
        gmail = MagicMock()
        gmail.get_current_history_id.return_value = "999"
        gmail.list_new_messages.return_value = [{"id": "msg1"}]
        gmail.get_message_metadata.return_value = _metadata("New User <user@company.io>")

        hubspot = MagicMock()
        hubspot.find_contact_by_email.return_value = None
        hubspot.create_contact.return_value = {"id": "hs123"}

        state = MagicMock(
            last_history_id=None, is_processed=MagicMock(return_value=False)
        )
        engine = SyncEngine(gmail, hubspot, state)

        results = engine.run_once()
        assert len(results) == 1
        assert results[0].status == STATUS_CREATED
        assert results[0].email == "user@company.io"
        assert results[0].contact_id == "hs123"
        hubspot.create_contact.assert_called_once()

    def test_updates_existing_contact(self):
        gmail = MagicMock()
        gmail.get_current_history_id.return_value = "1000"
        gmail.list_new_messages.return_value = [{"id": "msg2"}]
        gmail.get_message_metadata.return_value = _metadata("Jane Doe <jane@corp.com>")

        existing = {
            "id": "hs456",
            "properties": {"firstname": "", "lastname": "", "company": ""},
        }
        hubspot = MagicMock()
        hubspot.find_contact_by_email.return_value = existing
        hubspot.update_contact.return_value = {}

        state = MagicMock(
            last_history_id=None, is_processed=MagicMock(return_value=False)
        )
        engine = SyncEngine(gmail, hubspot, state)

        results = engine.run_once()
        assert results[0].status == STATUS_UPDATED
        assert results[0].contact_id == "hs456"
        hubspot.update_contact.assert_called_once()

    def test_skips_already_processed_message(self):
        gmail = MagicMock()
        gmail.get_current_history_id.return_value = "1001"
        gmail.list_new_messages.return_value = [{"id": "msg3"}]

        state = MagicMock(
            last_history_id=None, is_processed=MagicMock(return_value=True)
        )
        engine = _make_engine(gmail=gmail, state=state)

        results = engine.run_once()
        assert results == []

    def test_skips_message_without_from_header(self):
        gmail = MagicMock()
        gmail.get_current_history_id.return_value = "1002"
        gmail.list_new_messages.return_value = [{"id": "msg4"}]
        gmail.get_message_metadata.return_value = {"payload": {"headers": []}}

        state = MagicMock(
            last_history_id=None, is_processed=MagicMock(return_value=False)
        )
        engine = _make_engine(gmail=gmail, state=state)

        results = engine.run_once()
        assert results[0].status == STATUS_SKIPPED

    def test_error_on_gmail_failure(self):
        gmail = MagicMock()
        gmail.get_current_history_id.return_value = "1003"
        gmail.list_new_messages.return_value = [{"id": "msg5"}]
        gmail.get_message_metadata.side_effect = Exception("API error")

        state = MagicMock(
            last_history_id=None, is_processed=MagicMock(return_value=False)
        )
        engine = _make_engine(gmail=gmail, state=state)

        results = engine.run_once()
        assert results[0].status == STATUS_ERROR

    def test_no_duplicate_processing(self):
        gmail = MagicMock()
        gmail.get_current_history_id.return_value = "1004"
        gmail.list_new_messages.return_value = [{"id": "msg6"}, {"id": "msg6"}]
        gmail.get_message_metadata.return_value = _metadata("a@b.com")

        hubspot = MagicMock()
        hubspot.find_contact_by_email.return_value = None
        hubspot.create_contact.return_value = {"id": "hs1"}

        processed = set()

        def _is_processed(mid):
            return mid in processed

        def _mark(mid):
            processed.add(mid)

        state = MagicMock(last_history_id=None)
        state.is_processed.side_effect = _is_processed
        state.mark_processed.side_effect = _mark

        engine = SyncEngine(gmail, hubspot, state)
        results = engine.run_once()

        assert sum(1 for r in results if r.status == STATUS_CREATED) == 1
