"""Tests for HubSpot client logic (no real API calls)."""

from unittest.mock import MagicMock, patch

import pytest

from gmail_hubspot_sync.config import HubSpotConfig
from gmail_hubspot_sync.hubspot_client import HubSpotClient
from gmail_hubspot_sync.models import ContactInfo, SyncStatus


@pytest.fixture
def config():
    return HubSpotConfig(access_token="test_token_123")


@pytest.fixture
def client(config):
    return HubSpotClient(config)


class TestBuildProperties:
    def test_builds_all_fields(self, client):
        contact = ContactInfo(
            email="mario@acme.com", first_name="Mario", last_name="Rossi"
        )
        props = client._build_properties(contact)
        assert props["email"] == "mario@acme.com"
        assert props["firstname"] == "Mario"
        assert props["lastname"] == "Rossi"
        assert props["company"] == "Acme"
        assert props.get("leadsource") == "Gmail"

    def test_skips_existing_fields(self, client):
        contact = ContactInfo(email="mario@acme.com", first_name="Mario")
        existing = {
            "properties": {
                "firstname": "Mario",
                "company": "ACME Corp",
            }
        }
        props = client._build_properties(contact, existing)
        # firstname already in HubSpot → not overwritten
        assert "firstname" not in props
        # company already set → not overwritten
        assert "company" not in props

    def test_empty_contact_no_crash(self, client):
        contact = ContactInfo(email="x@gmail.com")
        props = client._build_properties(contact)
        assert "email" in props


class TestSyncContact:
    def test_creates_when_not_found(self, client):
        contact = ContactInfo(email="new@example.com", first_name="New", last_name="User")
        with patch.object(client, "find_contact_by_email", return_value=None), \
             patch.object(client, "create_contact") as mock_create:
            mock_create.return_value = MagicMock(status=SyncStatus.CREATED)
            result = client.sync_contact(contact)
            mock_create.assert_called_once_with(contact)

    def test_updates_when_found(self, client):
        contact = ContactInfo(email="existing@example.com")
        existing = {"id": "abc123", "properties": {}}
        with patch.object(client, "find_contact_by_email", return_value=existing), \
             patch.object(client, "update_contact") as mock_update:
            mock_update.return_value = MagicMock(status=SyncStatus.UPDATED)
            client.sync_contact(contact)
            mock_update.assert_called_once_with(contact, existing)
