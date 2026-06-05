"""Tests for the sync loop — all external I/O is mocked."""

from unittest.mock import MagicMock, patch, call
import pytest

from src.gmail_to_hubspot.sync import GmailHubSpotSync, _should_skip
from src.gmail_to_hubspot.hubspot_client import SyncResult, SyncStatus


# ---------------------------------------------------------------------------
# _should_skip
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("email", [
    "noreply@example.com",
    "no-reply@newsletter.io",
    "mailer-daemon@googlemail.com",
    "postmaster@domain.com",
    "notifications@github.com",
])
def test_should_skip_automated(email):
    assert _should_skip(email) is True


@pytest.mark.parametrize("email", [
    "mario@acme.com",
    "info@startup.io",
    "support@company.com",
])
def test_should_not_skip_real(email):
    assert _should_skip(email) is False


# ---------------------------------------------------------------------------
# GmailHubSpotSync.run_once
# ---------------------------------------------------------------------------

def _make_sync(tmp_path, messages, from_headers, upsert_returns):
    gmail = MagicMock()
    gmail.messages_since.return_value = iter(messages)
    gmail.get_from_header.side_effect = from_headers
    gmail.get_history_id.return_value = "99999"

    hub = MagicMock()
    hub.upsert_contact.side_effect = upsert_returns

    sync = GmailHubSpotSync(
        gmail=gmail,
        hubspot=hub,
        state_file=str(tmp_path / "state.json"),
        poll_interval=1,
    )
    return sync, gmail, hub


def test_run_once_creates_new_contact(tmp_path):
    messages = [{"id": "msg1"}]
    from_headers = ["Mario Rossi <mario@acme.com>"]
    upsert_returns = [SyncResult(SyncStatus.CREATED, "mario@acme.com", "42")]

    sync, gmail, hub = _make_sync(tmp_path, messages, from_headers, upsert_returns)
    results = sync.run_once()

    assert len(results) == 1
    assert results[0].status == SyncStatus.CREATED
    hub.upsert_contact.assert_called_once_with(
        email="mario@acme.com",
        first_name="Mario",
        last_name="Rossi",
        company="Acme",
    )


def test_run_once_skips_noreply(tmp_path):
    messages = [{"id": "msg2"}]
    from_headers = ["noreply@notifications.com"]
    upsert_returns = []

    sync, gmail, hub = _make_sync(tmp_path, messages, from_headers, upsert_returns)
    results = sync.run_once()

    assert results == []
    hub.upsert_contact.assert_not_called()


def test_run_once_skips_unparseable_header(tmp_path):
    messages = [{"id": "msg3"}]
    from_headers = [""]
    upsert_returns = []

    sync, gmail, hub = _make_sync(tmp_path, messages, from_headers, upsert_returns)
    results = sync.run_once()

    assert results == []


def test_run_once_persists_history_id(tmp_path):
    state_file = tmp_path / "state.json"
    messages = []
    sync, _, _ = _make_sync(tmp_path, messages, [], [])
    sync.run_once()

    import json
    saved = json.loads(state_file.read_text())
    assert saved["history_id"] == "99999"
