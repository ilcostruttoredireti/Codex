"""Test unitari per hubspot_client.py (logica sync, company detection)."""

import os
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("HUBSPOT_ACCESS_TOKEN", "test-token-dummy")

from gmail_hubspot_sync.hubspot_client import (
    HubSpotClient,
    SyncResult,
    SyncStatus,
    _company_from_domain,
)


# ── _company_from_domain ──────────────────────────────────────────────────────

class TestCompanyFromDomain:
    def test_business_domain(self):
        assert _company_from_domain("acme.com") == "acme.com"

    def test_gmail_ignored(self):
        assert _company_from_domain("gmail.com") == ""

    def test_yahoo_ignored(self):
        assert _company_from_domain("yahoo.it") == ""

    def test_libero_ignored(self):
        assert _company_from_domain("libero.it") == ""

    def test_protonmail_ignored(self):
        assert _company_from_domain("protonmail.com") == ""

    def test_business_subdomain(self):
        # I sottodomini sono trattati come aziendali
        assert _company_from_domain("mail.acme-corp.io") == "mail.acme-corp.io"


# ── HubSpotClient.sync_sender (mock API) ─────────────────────────────────────

def _make_client() -> HubSpotClient:
    """Crea un HubSpotClient con le API interne mockate."""
    with patch("hubspot.Client.create"):
        client = HubSpotClient.__new__(HubSpotClient)
        client._contacts_api = MagicMock()
        client._search_api = MagicMock()
        return client


class TestSyncSenderCreate:
    """Caso: contatto non esistente → deve essere creato."""

    def setup_method(self):
        self.client = _make_client()
        # Simula: nessun contatto trovato
        mock_result = MagicMock()
        mock_result.total = 0
        mock_result.results = []
        self.client._search_api.do_search.return_value = mock_result
        # Simula: creazione riuscita con id="12345"
        mock_contact = MagicMock()
        mock_contact.id = "12345"
        self.client._contacts_api.create.return_value = mock_contact

    def test_returns_created_status(self):
        result = self.client.sync_sender(
            email="mario@acme.com",
            first_name="Mario",
            last_name="Rossi",
            domain="acme.com",
        )
        assert result.status == SyncStatus.CREATED
        assert result.email == "mario@acme.com"
        assert result.contact_id == "12345"

    def test_create_called_once(self):
        self.client.sync_sender(email="mario@acme.com")
        assert self.client._contacts_api.create.call_count == 1

    def test_properties_include_email(self):
        self.client.sync_sender(email="mario@acme.com", first_name="Mario")
        call_kwargs = self.client._contacts_api.create.call_args
        props = call_kwargs[1]["simple_public_object_input_for_create"].properties
        assert props["email"] == "mario@acme.com"
        assert props["firstname"] == "Mario"


class TestSyncSenderUpdate:
    """Caso: contatto esistente con campi mancanti → deve essere aggiornato."""

    def setup_method(self):
        self.client = _make_client()
        # Simula: contatto trovato senza lastname
        mock_existing = MagicMock()
        mock_existing.id = "99"
        mock_existing.properties = {
            "email": "mario@acme.com",
            "firstname": "Mario",
            "lastname": "",
            "company": "",
        }
        mock_result = MagicMock()
        mock_result.total = 1
        mock_result.results = [mock_existing]
        self.client._search_api.do_search.return_value = mock_result

    def test_returns_updated_status(self):
        result = self.client.sync_sender(
            email="mario@acme.com",
            last_name="Rossi",
            domain="acme.com",
        )
        assert result.status == SyncStatus.UPDATED
        assert result.contact_id == "99"

    def test_update_called_once(self):
        self.client.sync_sender(email="mario@acme.com", last_name="Rossi")
        assert self.client._contacts_api.update.call_count == 1


class TestSyncSenderIgnored:
    """Caso: contatto esistente con tutti i dati → nessuna modifica."""

    def setup_method(self):
        self.client = _make_client()
        mock_existing = MagicMock()
        mock_existing.id = "77"
        mock_existing.properties = {
            "email": "mario@acme.com",
            "firstname": "Mario",
            "lastname": "Rossi",
            "company": "acme.com",
            "leadsource": "Gmail",
        }
        mock_result = MagicMock()
        mock_result.total = 1
        mock_result.results = [mock_existing]
        self.client._search_api.do_search.return_value = mock_result

    def test_returns_ignored_status(self):
        result = self.client.sync_sender(
            email="mario@acme.com",
            first_name="Mario",
            last_name="Rossi",
            domain="acme.com",
        )
        assert result.status == SyncStatus.IGNORED

    def test_update_not_called(self):
        self.client.sync_sender(
            email="mario@acme.com",
            first_name="Mario",
            last_name="Rossi",
            domain="acme.com",
        )
        self.client._contacts_api.update.assert_not_called()
