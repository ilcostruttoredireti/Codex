import pytest
from unittest.mock import MagicMock, patch
from src.config import Config, PERSONAL_DOMAINS
from src.models import EmailSender, ContactResult
from src.state import SyncState


def _make_config(**overrides) -> Config:
    defaults = dict(
        hubspot_api_token="test-token",
        gmail_credentials_file="credentials.json",
        gmail_token_file="token.json",
        poll_interval_seconds=60,
        state_file="sync_state.json",
        skip_personal_domains=False,
        personal_domains=PERSONAL_DOMAINS,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_sender(**overrides) -> EmailSender:
    defaults = dict(
        email="mario@acme.com",
        name="Mario Rossi",
        domain="acme.com",
        message_id="msg001",
        subject="Test email",
        date="Wed, 11 Jun 2026 10:00:00 +0000",
    )
    defaults.update(overrides)
    return EmailSender(**defaults)


@pytest.fixture
def syncer():
    from src.sync import GmailHubSpotSync
    config = _make_config()
    with patch("src.sync.GmailClient"), patch("src.sync.HubSpotClient") as MockHS:
        s = GmailHubSpotSync(config)
        s.hubspot = MockHS.return_value
        s.gmail = MagicMock()
        return s


def test_sync_creates_new_contact(syncer):
    syncer.hubspot.search_contact_by_email.return_value = None
    syncer.hubspot.create_contact.return_value = {"id": "hs-123"}
    syncer.hubspot.add_note_to_contact.return_value = "note-1"

    result = syncer._sync_sender(_make_sender())

    assert result.status == "created"
    assert result.contact_id == "hs-123"
    syncer.hubspot.create_contact.assert_called_once()
    props = syncer.hubspot.create_contact.call_args[0][0]
    assert props["email"] == "mario@acme.com"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"


def test_sync_updates_existing_contact_missing_fields(syncer):
    syncer.hubspot.search_contact_by_email.return_value = {
        "id": "hs-456",
        "properties": {"email": "mario@acme.com", "firstname": "", "company": ""},
    }
    syncer.hubspot.update_contact.return_value = {}
    syncer.hubspot.add_note_to_contact.return_value = "note-2"

    result = syncer._sync_sender(_make_sender())

    assert result.status == "updated"
    assert result.contact_id == "hs-456"
    syncer.hubspot.update_contact.assert_called_once()


def test_sync_ignores_contact_with_all_fields_present(syncer):
    syncer.hubspot.search_contact_by_email.return_value = {
        "id": "hs-789",
        "properties": {
            "email": "mario@acme.com",
            "firstname": "Mario",
            "lastname": "Rossi",
            "company": "Acme",
        },
    }
    syncer.hubspot.add_note_to_contact.return_value = "note-3"

    result = syncer._sync_sender(_make_sender())

    assert result.status == "ignored"
    syncer.hubspot.update_contact.assert_not_called()


def test_skip_personal_domain_when_enabled(syncer):
    syncer.config.skip_personal_domains = True
    sender = _make_sender(email="mario@gmail.com", domain="gmail.com")

    result = syncer._sync_sender(sender)

    assert result.status == "ignored"
    assert "personale" in result.reason
    syncer.hubspot.search_contact_by_email.assert_not_called()


def test_run_cycle_skips_already_processed(syncer):
    state = SyncState(
        last_history_id="h1",
        processed_message_ids=["msg001"],
    )
    syncer.gmail.get_new_messages_since.return_value = [{"id": "msg001"}]
    syncer.gmail.get_current_history_id.return_value = "h2"

    _, results = syncer.run_cycle(state)

    assert results == []
    syncer.hubspot.search_contact_by_email.assert_not_called()


def test_run_cycle_first_run_uses_recent_messages(syncer):
    state = SyncState()  # last_history_id is None
    syncer.gmail.get_recent_inbox_messages.return_value = []
    syncer.gmail.get_current_history_id.return_value = "h1"

    new_state, _ = syncer.run_cycle(state)

    syncer.gmail.get_recent_inbox_messages.assert_called_once()
    assert new_state.last_history_id == "h1"


def test_run_cycle_advances_history_id(syncer):
    state = SyncState(last_history_id="old-h")
    syncer.gmail.get_new_messages_since.return_value = []
    syncer.gmail.get_current_history_id.return_value = "new-h"

    new_state, _ = syncer.run_cycle(state)

    assert new_state.last_history_id == "new-h"
