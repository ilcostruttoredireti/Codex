from unittest.mock import MagicMock, patch

import pytest
from contact_processor import ContactProcessor, SyncStatus
from hubspot_client import HubSpotClient


@pytest.fixture
def mock_hubspot():
    return MagicMock(spec=HubSpotClient)


@pytest.fixture
def processor(mock_hubspot):
    return ContactProcessor(mock_hubspot)


def test_create_new_contact(processor, mock_hubspot):
    mock_hubspot.find_contact_by_email.return_value = None
    mock_hubspot.create_contact.return_value = "12345"

    result = processor.process("Mario Rossi <mario@acme.com>")

    assert result.status == SyncStatus.CREATED
    assert result.email == "mario@acme.com"
    assert result.contact_id == "12345"
    mock_hubspot.create_contact.assert_called_once()


def test_update_existing_contact(processor, mock_hubspot):
    mock_hubspot.find_contact_by_email.return_value = {
        "id": "99",
        "properties": {"email": "mario@acme.com", "firstname": "", "company": ""},
    }
    mock_hubspot.update_contact.return_value = True

    result = processor.process("Mario Rossi <mario@acme.com>")

    assert result.status == SyncStatus.UPDATED
    assert result.contact_id == "99"


def test_ignore_noreply(processor, mock_hubspot):
    result = processor.process("noreply@service.com")
    assert result.status == SyncStatus.IGNORED
    mock_hubspot.find_contact_by_email.assert_not_called()


def test_ignore_gmail_domain(processor, mock_hubspot):
    result = processor.process("someone@gmail.com")
    assert result.status == SyncStatus.IGNORED


def test_ignore_invalid_email(processor, mock_hubspot):
    result = processor.process("not-an-email")
    assert result.status == SyncStatus.IGNORED


def test_no_update_when_fields_complete(processor, mock_hubspot):
    mock_hubspot.find_contact_by_email.return_value = {
        "id": "77",
        "properties": {
            "email": "luigi@corp.io",
            "firstname": "Luigi",
            "lastname": "Bianchi",
            "company": "Corp",
        },
    }
    mock_hubspot.update_contact.return_value = False

    result = processor.process("Luigi Bianchi <luigi@corp.io>")
    assert result.status == SyncStatus.IGNORED
    assert result.reason == "No new fields to update"
