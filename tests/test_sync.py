from unittest.mock import MagicMock, patch

import pytest

from models import SyncStatus
from sync import GmailHubSpotSync


def _make_syncer(gmail=None, hubspot=None):
    gmail = gmail or MagicMock()
    hubspot = hubspot or MagicMock()
    return GmailHubSpotSync(gmail=gmail, hubspot=hubspot, poll_interval=1)


class TestProcessMessage:
    def test_creates_new_contact(self):
        gmail = MagicMock()
        gmail.get_message_headers.return_value = {
            "From": "Mario Rossi <mario@acme.com>",
            "Subject": "Hello",
        }
        hubspot = MagicMock()
        hubspot.find_contact.return_value = None
        hubspot.create_contact.return_value = {"id": "42"}

        syncer = _make_syncer(gmail, hubspot)
        result = syncer.process_message("msg1")

        assert result.status == SyncStatus.CREATED
        assert result.email == "mario@acme.com"
        assert result.contact_id == "42"
        hubspot.create_contact.assert_called_once()
        hubspot.log_email_activity.assert_called_once_with("42", "Hello", "mario@acme.com")

    def test_updates_existing_contact(self):
        gmail = MagicMock()
        gmail.get_message_headers.return_value = {
            "From": "Giulia <giulia@startup.io>",
            "Subject": "Re: meeting",
        }
        existing = {"id": "99", "properties": {"email": "giulia@startup.io", "firstname": "", "lastname": ""}}
        hubspot = MagicMock()
        hubspot.find_contact.return_value = existing

        syncer = _make_syncer(gmail, hubspot)
        result = syncer.process_message("msg2")

        assert result.status == SyncStatus.UPDATED
        assert result.contact_id == "99"
        hubspot.create_contact.assert_not_called()
        hubspot.update_contact.assert_called_once()

    def test_ignores_missing_from_header(self):
        gmail = MagicMock()
        gmail.get_message_headers.return_value = {"Subject": "Hello"}
        syncer = _make_syncer(gmail)
        result = syncer.process_message("msg3")
        assert result.status == SyncStatus.IGNORED

    def test_ignores_noreply_sender(self):
        gmail = MagicMock()
        gmail.get_message_headers.return_value = {"From": "noreply@service.com", "Subject": "Notification"}
        syncer = _make_syncer(gmail)
        result = syncer.process_message("msg4")
        assert result.status == SyncStatus.IGNORED

    def test_error_when_header_fetch_fails(self):
        gmail = MagicMock()
        gmail.get_message_headers.return_value = None
        syncer = _make_syncer(gmail)
        result = syncer.process_message("msg5")
        assert result.status == SyncStatus.ERROR

    def test_error_when_hubspot_create_fails(self):
        gmail = MagicMock()
        gmail.get_message_headers.return_value = {"From": "user@company.com", "Subject": "Hi"}
        hubspot = MagicMock()
        hubspot.find_contact.return_value = None
        hubspot.create_contact.return_value = None
        syncer = _make_syncer(gmail, hubspot)
        result = syncer.process_message("msg6")
        assert result.status == SyncStatus.ERROR


class TestRunOnce:
    def test_marks_all_messages_as_synced(self):
        gmail = MagicMock()
        gmail.fetch_unsynced_messages.return_value = [{"id": "a"}, {"id": "b"}]
        gmail.get_message_headers.return_value = {
            "From": "user@corp.com",
            "Subject": "test",
        }
        hubspot = MagicMock()
        hubspot.find_contact.return_value = None
        hubspot.create_contact.return_value = {"id": "1"}

        syncer = _make_syncer(gmail, hubspot)
        results = syncer.run_once()

        assert len(results) == 2
        assert gmail.mark_as_synced.call_count == 2

    def test_empty_inbox_returns_no_results(self):
        gmail = MagicMock()
        gmail.fetch_unsynced_messages.return_value = []
        syncer = _make_syncer(gmail)
        assert syncer.run_once() == []
