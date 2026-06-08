"""Unit tests for SyncEngine — Gmail and HubSpot are mocked."""
from unittest.mock import MagicMock, patch

import pytest

from sync.config import Config
from sync.sync_engine import SyncEngine


def _make_engine(dry_run=False):
    cfg = Config()
    cfg.HUBSPOT_API_KEY = "fake"
    cfg.GMAIL_CREDENTIALS_PATH = "fake_creds.json"
    cfg.GMAIL_TOKEN_PATH = "fake_token.json"
    cfg.STATE_FILE = "/tmp/test_sync_state.json"
    cfg.BACKFILL_DAYS = 0

    engine = SyncEngine(cfg, dry_run=dry_run)
    # Replace real clients with mocks
    engine._gmail = MagicMock()
    engine._hubspot = MagicMock()
    engine._state = MagicMock()
    engine._own_email = "me@mycompany.com"
    return engine


class TestProcessMessage:
    def test_self_email_is_ignored(self):
        engine = _make_engine()
        engine._state.is_processed.return_value = False
        engine._gmail.get_message_metadata.return_value = {
            "message_id": "m1",
            "from_header": "Me <me@mycompany.com>",
            "subject": "test",
            "date": "Mon, 1 Jan 2024 10:00:00 +0000",
        }
        result = engine._process_message("m1")
        assert result.status == "Ignorato"
        assert "propria" in result.reason

    def test_noreply_is_ignored(self):
        engine = _make_engine()
        engine._gmail.get_message_metadata.return_value = {
            "message_id": "m2",
            "from_header": "noreply@service.com",
            "subject": "Welcome",
            "date": "Mon, 1 Jan 2024",
        }
        engine._gmail.should_skip.return_value = True
        result = engine._process_message("m2")
        assert result.status == "Ignorato"

    def test_new_contact_is_created(self):
        engine = _make_engine()
        engine._state.is_processed.return_value = False
        engine._gmail.get_message_metadata.return_value = {
            "message_id": "m3",
            "from_header": "Luca Bianchi <luca@startup.io>",
            "subject": "Ciao",
            "date": "Mon, 1 Jan 2024",
        }
        engine._gmail.should_skip = MagicMock(return_value=False)
        engine._hubspot.find_contact_by_email.return_value = None
        engine._hubspot.create_contact.return_value = {"id": "12345"}

        result = engine._process_message("m3")

        assert result.status == "Creato"
        assert result.hubspot_id == "12345"
        assert result.email == "luca@startup.io"
        engine._hubspot.create_contact.assert_called_once()

    def test_existing_contact_is_updated(self):
        engine = _make_engine()
        engine._state.is_processed.return_value = False
        engine._gmail.get_message_metadata.return_value = {
            "message_id": "m4",
            "from_header": "Luca Bianchi <luca@startup.io>",
            "subject": "Re: meeting",
            "date": "Mon, 1 Jan 2024",
        }
        engine._gmail.should_skip = MagicMock(return_value=False)
        engine._hubspot.find_contact_by_email.return_value = {
            "id": "99",
            "properties": {"email": "luca@startup.io", "firstname": "", "lastname": "", "company": ""},
        }

        result = engine._process_message("m4")

        assert result.status == "Aggiornato"
        assert result.hubspot_id == "99"

    def test_dry_run_does_not_call_hubspot(self):
        engine = _make_engine(dry_run=True)
        engine._state.is_processed.return_value = False
        engine._gmail.get_message_metadata.return_value = {
            "message_id": "m5",
            "from_header": "Test User <test@company.com>",
            "subject": "hello",
            "date": "Mon, 1 Jan 2024",
        }
        engine._gmail.should_skip = MagicMock(return_value=False)

        engine._process_message("m5")

        engine._hubspot.create_contact.assert_not_called()
        engine._hubspot.update_contact.assert_not_called()
