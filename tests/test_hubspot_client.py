import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import MagicMock, patch
from gmail_hubspot_sync.hubspot_client import _domain_to_company, _build_properties, sync_contact
from gmail_hubspot_sync.hubspot_client import SyncResult


def test_domain_to_company():
    assert _domain_to_company("acme.com") == "Acme"
    assert _domain_to_company("google.it") == "Google"


def test_build_properties_full():
    props = _build_properties("m@acme.com", "Mario", "Rossi", "acme.com")
    assert props["email"] == "m@acme.com"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"


def test_build_properties_partial():
    props = _build_properties("m@acme.com", "", "", "acme.com")
    assert "firstname" not in props
    assert "lastname" not in props


def test_sync_contact_creates_new():
    mock_contact = MagicMock()
    mock_contact.id = "12345"

    with patch("gmail_hubspot_sync.hubspot_client._client") as mock_client_fn, \
         patch("gmail_hubspot_sync.hubspot_client._find_contact", return_value=None), \
         patch("gmail_hubspot_sync.hubspot_client._add_note"):
        mock_client = MagicMock()
        mock_client_fn.return_value = mock_client
        mock_client.crm.contacts.basic_api.create.return_value = mock_contact

        result = sync_contact("new@test.com", "New", "User", "test.com", "Hello")

    assert result.status == "created"
    assert result.contact_id == "12345"


def test_sync_contact_skips_existing_no_updates():
    existing = MagicMock()
    existing.id = "99"
    existing.properties = {"firstname": "Mario", "lastname": "Rossi", "company": "Acme"}

    with patch("gmail_hubspot_sync.hubspot_client._client"), \
         patch("gmail_hubspot_sync.hubspot_client._find_contact", return_value=existing), \
         patch("gmail_hubspot_sync.hubspot_client._add_note"):
        result = sync_contact("m@acme.com", "Mario", "Rossi", "acme.com", "Re: test")

    assert result.status == "skipped"
    assert result.contact_id == "99"
