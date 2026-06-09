from unittest.mock import MagicMock, patch

import pytest

from gmail_hubspot_sync.models import ContactInfo, SyncStatus
from gmail_hubspot_sync.sync_engine import SyncEngine


def _make_engine(gmail_mock, hubspot_mock):
    return SyncEngine(gmail_mock, hubspot_mock, max_per_cycle=10)


def _gmail_with_message(from_header: str, msg_id: str = "msg1") -> MagicMock:
    gmail = MagicMock()
    gmail.get_unprocessed_inbox_messages.return_value = [{"id": msg_id}]
    gmail.get_message_sender.return_value = {"from": from_header, "message_id": msg_id}
    return gmail


class TestSyncEngine:
    def test_creates_new_contact(self):
        gmail = _gmail_with_message('"Mario Rossi" <mario@acme.com>')
        hubspot = MagicMock()
        hubspot.find_contact_by_email.return_value = None
        hubspot.create_contact.return_value = "hs-123"

        results = _make_engine(gmail, hubspot).run_cycle()

        assert len(results) == 1
        r = results[0]
        assert r.status == SyncStatus.CREATED
        assert r.email == "mario@acme.com"
        assert r.hubspot_id == "hs-123"
        hubspot.create_contact.assert_called_once()

    def test_updates_existing_contact(self):
        gmail = _gmail_with_message('"Luca Bianchi" <luca@beta.it>')
        hubspot = MagicMock()
        existing = {"id": "hs-456", "properties": {"firstname": "", "lastname": "", "company": ""}}
        hubspot.find_contact_by_email.return_value = existing

        results = _make_engine(gmail, hubspot).run_cycle()

        assert results[0].status == SyncStatus.UPDATED
        assert results[0].hubspot_id == "hs-456"
        hubspot.update_contact.assert_called_once()
        hubspot.create_contact.assert_not_called()

    def test_ignored_sender_not_sent_to_hubspot(self):
        gmail = _gmail_with_message("no-reply@service.com")
        hubspot = MagicMock()

        results = _make_engine(gmail, hubspot).run_cycle()

        assert results[0].status == SyncStatus.IGNORED
        hubspot.find_contact_by_email.assert_not_called()

    def test_empty_inbox_returns_empty_list(self):
        gmail = MagicMock()
        gmail.get_unprocessed_inbox_messages.return_value = []
        hubspot = MagicMock()

        results = _make_engine(gmail, hubspot).run_cycle()

        assert results == []

    def test_message_marked_processed_even_on_hubspot_error(self):
        gmail = _gmail_with_message('"Test User" <test@company.com>', "msg99")
        hubspot = MagicMock()
        hubspot.find_contact_by_email.side_effect = RuntimeError("network error")

        results = _make_engine(gmail, hubspot).run_cycle()

        assert results[0].status == SyncStatus.IGNORED
        gmail.mark_as_processed.assert_called_once_with("msg99")

    def test_hubspot_create_failure_returns_ignored(self):
        gmail = _gmail_with_message("alice@startup.io")
        hubspot = MagicMock()
        hubspot.find_contact_by_email.return_value = None
        hubspot.create_contact.side_effect = Exception("rate limit")

        results = _make_engine(gmail, hubspot).run_cycle()

        assert results[0].status == SyncStatus.IGNORED
        assert "rate limit" in results[0].reason
