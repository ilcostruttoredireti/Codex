"""Unit tests for the sync logic using a mock HubSpot client."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
import pytest
from models import SyncStatus
from sync import sync_message


def _make_message(from_header: str, subject: str = "Ciao", msg_id: str = "abc") -> dict:
    return {"from": from_header, "subject": subject, "id": msg_id}


def _mock_hs(existing=None):
    hs = MagicMock()
    hs.find_contact_by_email.return_value = existing
    hs.create_contact.return_value = "hs-999"
    return hs


class TestSyncMessage:
    def test_creates_new_contact(self):
        hs = _mock_hs(existing=None)
        result = sync_message(_make_message("Mario Rossi <mario@acme.com>"), hs)
        assert result.status == SyncStatus.CREATED
        assert result.email == "mario@acme.com"
        assert result.hubspot_id == "hs-999"
        hs.create_contact.assert_called_once()

    def test_updates_existing_contact(self):
        existing = {"id": "hs-123", "properties": {"firstname": "Mario", "lastname": ""}}
        hs = _mock_hs(existing=existing)
        result = sync_message(_make_message("Mario Rossi <mario@acme.com>"), hs)
        assert result.status == SyncStatus.UPDATED
        assert result.hubspot_id == "hs-123"
        hs.update_contact.assert_called_once()

    def test_skips_noreply(self):
        hs = _mock_hs()
        result = sync_message(_make_message("noreply@google.com"), hs)
        assert result.status == SyncStatus.SKIPPED
        hs.find_contact_by_email.assert_not_called()

    def test_skips_empty_from(self):
        hs = _mock_hs()
        result = sync_message(_make_message(""), hs)
        assert result.status == SyncStatus.SKIPPED

    def test_handles_hubspot_error(self):
        hs = _mock_hs()
        hs.find_contact_by_email.side_effect = Exception("API error")
        result = sync_message(_make_message("user@firm.com"), hs)
        assert result.status == SyncStatus.SKIPPED
        assert "API error" in result.error

    def test_adds_timeline_note(self):
        hs = _mock_hs(existing=None)
        sync_message(_make_message("luca@corp.io", subject="Progetto X"), hs)
        hs.add_email_received_note.assert_called_once_with("hs-999", "Progetto X", "luca@corp.io")
