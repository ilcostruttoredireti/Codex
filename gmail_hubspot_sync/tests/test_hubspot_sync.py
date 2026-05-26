"""Test per hubspot_sync.py (unit test con mock HTTP)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch, MagicMock
from parsing_utils import parse_from_header, extract_domain, domain_to_company


# ── Helpers per creare SenderInfo senza importare gmail_monitor ──────────────

class FakeSender:
    """Stub di SenderInfo per testare HubSpotSync senza dipendenze Google."""
    def __init__(self, **kwargs):
        defaults = dict(
            message_id="msg_001",
            raw_from="Mario Rossi <mario@acme.it>",
            email="mario@acme.it",
            first_name="Mario",
            last_name="Rossi",
            full_name="Mario Rossi",
            domain="acme.it",
            company="Acme",
            subject="Ciao",
            date="Mon, 1 Jan 2024",
        )
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


# Monkey-patch SenderInfo so HubSpotSync accepts FakeSender
import parsing_utils


@pytest.fixture
def hubspot():
    # Import lazy di HubSpotSync (no google libs needed)
    from hubspot_sync import HubSpotSync
    return HubSpotSync(api_key="test-key")


class TestSyncSenderIgnoredDomain:
    def test_gmail_domain_ignored(self, hubspot):
        sender = FakeSender(email="user@gmail.com", domain="gmail.com")
        result = hubspot.sync_sender(sender)
        from hubspot_sync import SyncStatus
        assert result.status == SyncStatus.IGNORATO
        assert "personale" in result.reason

    def test_yahoo_ignored(self, hubspot):
        sender = FakeSender(email="user@yahoo.com", domain="yahoo.com")
        result = hubspot.sync_sender(sender)
        from hubspot_sync import SyncStatus
        assert result.status == SyncStatus.IGNORATO

    def test_libero_ignored(self, hubspot):
        sender = FakeSender(email="user@libero.it", domain="libero.it")
        result = hubspot.sync_sender(sender)
        from hubspot_sync import SyncStatus
        assert result.status == SyncStatus.IGNORATO


class TestSyncSenderCreate:
    def test_creates_new_contact(self, hubspot):
        from hubspot_sync import SyncStatus
        sender = FakeSender()

        with patch.object(hubspot, "find_contact_by_email", return_value=None), \
             patch.object(hubspot, "create_contact", return_value="999"), \
             patch.object(hubspot, "add_tag_to_contact"), \
             patch.object(hubspot, "log_email_activity"):

            result = hubspot.sync_sender(sender)

        assert result.status == SyncStatus.CREATO
        assert result.contact_id == "999"
        assert result.email == "mario@acme.it"

    def test_create_failure_returns_error(self, hubspot):
        from hubspot_sync import SyncStatus
        sender = FakeSender()

        with patch.object(hubspot, "find_contact_by_email", return_value=None), \
             patch.object(hubspot, "create_contact", return_value=None):

            result = hubspot.sync_sender(sender)

        assert result.status == SyncStatus.ERRORE


class TestSyncSenderUpdate:
    def _existing_contact(self, **props):
        return {
            "id": "123",
            "properties": {"email": "mario@acme.it", **props},
        }

    def test_updates_empty_fields(self, hubspot):
        from hubspot_sync import SyncStatus
        sender = FakeSender()
        existing = self._existing_contact(firstname="", lastname="", company="")

        with patch.object(hubspot, "find_contact_by_email", return_value=existing), \
             patch.object(hubspot, "update_contact", return_value=True) as mock_update, \
             patch.object(hubspot, "add_tag_to_contact"), \
             patch.object(hubspot, "log_email_activity"):

            result = hubspot.sync_sender(sender)

        assert result.status == SyncStatus.AGGIORNATO
        assert result.contact_id == "123"
        mock_update.assert_called_once()

    def test_no_update_when_all_fields_present(self, hubspot):
        from hubspot_sync import SyncStatus
        sender = FakeSender()
        existing = self._existing_contact(
            firstname="Mario", lastname="Rossi", company="Acme"
        )

        with patch.object(hubspot, "find_contact_by_email", return_value=existing), \
             patch.object(hubspot, "update_contact", return_value=False), \
             patch.object(hubspot, "add_tag_to_contact"), \
             patch.object(hubspot, "log_email_activity"):

            result = hubspot.sync_sender(sender)

        assert result.status == SyncStatus.IGNORATO


class TestBuildProperties:
    def test_create_includes_all_fields(self, hubspot):
        sender = FakeSender()
        props = hubspot._build_properties(sender)
        assert props["email"] == "mario@acme.it"
        assert props["firstname"] == "Mario"
        assert props["lastname"] == "Rossi"
        assert props["company"] == "Acme"
        assert props["leadsource"] == "Gmail"

    def test_update_skips_existing_fields(self, hubspot):
        sender = FakeSender()
        existing = {
            "properties": {"firstname": "ESISTENTE", "lastname": "", "company": ""}
        }
        props = hubspot._build_properties(sender, existing=existing)
        # firstname già presente → NON sovrascrivere
        assert "firstname" not in props
        # lastname vuoto → aggiorna
        assert props["lastname"] == "Rossi"
        # company vuota → aggiorna
        assert props["company"] == "Acme"

    def test_create_without_name_omits_empty_fields(self, hubspot):
        sender = FakeSender(first_name="", last_name="", company="")
        props = hubspot._build_properties(sender)
        assert "firstname" not in props
        assert "lastname" not in props
        assert "company" not in props
        assert props["email"] == "mario@acme.it"
