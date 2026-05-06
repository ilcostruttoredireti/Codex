"""Unit tests for the sync logic (no external API calls)."""

import pytest
from unittest.mock import MagicMock, patch

from utils import parse_sender, domain_to_company
from hubspot_client import SyncStatus, SyncResult, HubSpotClient


# ── parse_sender ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (
        "Mario Rossi <mario.rossi@acme.com>",
        {"email": "mario.rossi@acme.com", "first_name": "Mario", "last_name": "Rossi", "domain": "acme.com"},
    ),
    (
        '"Giulia Bianchi" <g.bianchi@example.org>',
        {"email": "g.bianchi@example.org", "first_name": "Giulia", "last_name": "Bianchi", "domain": "example.org"},
    ),
    (
        "noreply@github.com",
        {"email": "noreply@github.com", "first_name": "", "last_name": "", "domain": "github.com"},
    ),
    (
        "Luca <luca@startup.io>",
        {"email": "luca@startup.io", "first_name": "Luca", "last_name": "", "domain": "startup.io"},
    ),
])
def test_parse_sender(raw, expected):
    assert parse_sender(raw) == expected


# ── domain_to_company ────────────────────────────────────────────────────────

@pytest.mark.parametrize("domain,expected", [
    ("acme.com", "Acme"),
    ("startup.io", "Startup"),
    ("gmail.com", ""),
    ("hotmail.com", ""),
    ("www.example.co.uk", "Example"),
    ("", ""),
])
def test_domain_to_company(domain, expected):
    assert domain_to_company(domain) == expected


# ── HubSpotClient.sync_contact ───────────────────────────────────────────────

def _make_hubspot_client():
    client = HubSpotClient.__new__(HubSpotClient)
    client._client = MagicMock()
    return client


def test_sync_contact_creates_new():
    client = _make_hubspot_client()
    client._find_contact = MagicMock(return_value=None)
    client._create_contact = MagicMock(return_value="CT001")
    client._log_email_activity = MagicMock()

    result = client.sync_contact("mario@acme.com", "Mario", "Rossi", "acme.com")

    assert result.status == SyncStatus.CREATED
    assert result.contact_id == "CT001"
    client._create_contact.assert_called_once_with("mario@acme.com", "Mario", "Rossi", "acme.com")


def test_sync_contact_updates_existing():
    client = _make_hubspot_client()
    client._find_contact = MagicMock(return_value={"id": "CT002", "properties": {"firstname": ""}})
    client._update_contact = MagicMock(return_value=True)
    client._log_email_activity = MagicMock()

    result = client.sync_contact("giulia@acme.com", "Giulia", "Bianchi", "acme.com")

    assert result.status == SyncStatus.UPDATED
    assert result.contact_id == "CT002"


def test_sync_contact_ignored_when_no_update_needed():
    client = _make_hubspot_client()
    existing = {"id": "CT003", "properties": {"firstname": "Luca", "lastname": "Verdi", "company": "Acme"}}
    client._find_contact = MagicMock(return_value=existing)
    client._update_contact = MagicMock(return_value=False)
    client._log_email_activity = MagicMock()

    result = client.sync_contact("luca@acme.com", "Luca", "Verdi", "acme.com")

    assert result.status == SyncStatus.IGNORED


def test_sync_contact_skips_invalid_email():
    client = _make_hubspot_client()
    result = client.sync_contact("not-an-email", "", "", "")
    assert result.status == SyncStatus.IGNORED
    assert result.contact_id == ""
