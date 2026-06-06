"""Integration-style tests for the sync loop using mocks."""

import pytest
from unittest.mock import MagicMock, patch

from gmail_hubspot_sync.sync import run_once, SyncConfig


@pytest.fixture
def config():
    return SyncConfig(
        google_client_id="fake_id",
        google_client_secret="fake_secret",
        google_refresh_token="fake_refresh",
        hubspot_access_token="fake_hs_token",
        skip_domains={"gmail.com"},
        state_path=":memory:",
    )


@patch("gmail_hubspot_sync.sync.SyncState")
@patch("gmail_hubspot_sync.sync.build_client")
@patch("gmail_hubspot_sync.sync.build_service")
@patch("gmail_hubspot_sync.sync.fetch_new_messages")
@patch("gmail_hubspot_sync.sync.get_current_history_id")
@patch("gmail_hubspot_sync.sync.find_contact_by_email")
@patch("gmail_hubspot_sync.sync.create_contact")
def test_new_contact_created(
    mock_create, mock_find, mock_hist, mock_fetch,
    mock_gmail, mock_hs, mock_state, config
):
    mock_state.return_value.is_processed.return_value = False
    mock_state.return_value.history_id = None
    mock_hist.return_value = "12345"
    mock_fetch.return_value = iter([
        ("msg1", {"email": "user@company.com", "first_name": "User",
                  "last_name": "", "domain": "company.com", "display_name": "User"}),
    ])
    mock_find.return_value = None
    mock_create.return_value = "hs_001"

    results = run_once(config)

    assert len(results) == 1
    assert results[0].status == "CREATED"
    assert results[0].email == "user@company.com"
    assert results[0].contact_id == "hs_001"


@patch("gmail_hubspot_sync.sync.SyncState")
@patch("gmail_hubspot_sync.sync.build_client")
@patch("gmail_hubspot_sync.sync.build_service")
@patch("gmail_hubspot_sync.sync.fetch_new_messages")
@patch("gmail_hubspot_sync.sync.get_current_history_id")
@patch("gmail_hubspot_sync.sync.find_contact_by_email")
@patch("gmail_hubspot_sync.sync.update_contact")
def test_existing_contact_updated(
    mock_update, mock_find, mock_hist, mock_fetch,
    mock_gmail, mock_hs, mock_state, config
):
    existing = MagicMock()
    existing.id = "hs_999"
    mock_state.return_value.is_processed.return_value = False
    mock_state.return_value.history_id = None
    mock_hist.return_value = "12345"
    mock_fetch.return_value = iter([
        ("msg2", {"email": "known@corp.com", "first_name": "Known",
                  "last_name": "User", "domain": "corp.com", "display_name": "Known User"}),
    ])
    mock_find.return_value = existing
    mock_update.return_value = "hs_999"

    results = run_once(config)

    assert results[0].status == "UPDATED"
    assert results[0].contact_id == "hs_999"


@patch("gmail_hubspot_sync.sync.SyncState")
@patch("gmail_hubspot_sync.sync.build_client")
@patch("gmail_hubspot_sync.sync.build_service")
@patch("gmail_hubspot_sync.sync.fetch_new_messages")
@patch("gmail_hubspot_sync.sync.get_current_history_id")
def test_already_processed_skipped(
    mock_hist, mock_fetch, mock_gmail, mock_hs, mock_state, config
):
    mock_state.return_value.is_processed.return_value = True
    mock_state.return_value.history_id = None
    mock_hist.return_value = "12345"
    mock_fetch.return_value = iter([
        ("msg3", {"email": "dup@dup.com", "first_name": "",
                  "last_name": "", "domain": "dup.com", "display_name": ""}),
    ])

    results = run_once(config)

    assert results[0].status == "SKIPPED"
    assert "already processed" in results[0].detail
