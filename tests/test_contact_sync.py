"""Unit tests for ContactSyncService (no real API calls)."""

from unittest.mock import MagicMock, patch
import pytest

from gmail_hubspot_sync.models import SenderInfo
from gmail_hubspot_sync.contact_sync import ContactSyncService, SyncStatus


def _make_sender(email="user@acme.com", first="John", last="Doe",
                 domain="acme.com", company="Acme", subject="Hello"):
    return SenderInfo(
        email=email, first_name=first, last_name=last,
        domain=domain, company=company, message_id="msg_001", subject=subject,
    )


@pytest.fixture()
def mock_hs():
    hs = MagicMock()
    hs.find_contact_by_email.return_value = None
    hs.create_contact.return_value = "hs_123"
    hs.update_contact.return_value = False
    return hs


def test_new_contact_is_created(mock_hs):
    svc = ContactSyncService(hubspot_client=mock_hs)
    result = svc.process(_make_sender(), log_activity=False)

    assert result.status == SyncStatus.CREATED
    assert result.contact_id == "hs_123"
    mock_hs.create_contact.assert_called_once()


def test_existing_contact_no_changes_is_skipped(mock_hs):
    mock_hs.find_contact_by_email.return_value = {
        "id": "hs_456",
        "properties": {"firstname": "John", "lastname": "Doe", "company": "Acme"},
    }
    mock_hs.update_contact.return_value = False

    svc = ContactSyncService(hubspot_client=mock_hs)
    result = svc.process(_make_sender(), log_activity=False)

    assert result.status == SyncStatus.SKIPPED
    assert result.contact_id == "hs_456"


def test_existing_contact_missing_fields_is_updated(mock_hs):
    mock_hs.find_contact_by_email.return_value = {
        "id": "hs_456",
        "properties": {"firstname": "", "lastname": "", "company": ""},
    }
    mock_hs.update_contact.return_value = True

    svc = ContactSyncService(hubspot_client=mock_hs)
    result = svc.process(_make_sender(), log_activity=False)

    assert result.status == SyncStatus.UPDATED


def test_skip_domain(mock_hs):
    sender = _make_sender(email="bot@noreply.com", domain="noreply.com")
    svc = ContactSyncService(hubspot_client=mock_hs)
    result = svc.process(sender, log_activity=False)

    assert result.status == SyncStatus.SKIPPED
    mock_hs.create_contact.assert_not_called()


def test_dedup_within_run(mock_hs):
    svc = ContactSyncService(hubspot_client=mock_hs)
    svc.process(_make_sender(), log_activity=False)
    result2 = svc.process(_make_sender(), log_activity=False)

    assert result2.status == SyncStatus.SKIPPED
    assert mock_hs.create_contact.call_count == 1


def test_log_activity_called_on_create(mock_hs):
    svc = ContactSyncService(hubspot_client=mock_hs)
    svc.process(_make_sender(), log_activity=True)

    mock_hs.log_email_activity.assert_called_once_with(
        contact_id="hs_123",
        email="user@acme.com",
        subject="Hello",
        message_id="msg_001",
    )


def test_hubspot_create_failure_returns_skipped(mock_hs):
    mock_hs.create_contact.return_value = None
    svc = ContactSyncService(hubspot_client=mock_hs)
    result = svc.process(_make_sender(), log_activity=False)

    assert result.status == SyncStatus.SKIPPED
    assert result.contact_id is None
