"""Tests for Gmail email parsing logic."""

import pytest
from gmail_hubspot_sync.gmail_client import GmailClient, _parse_from_header


class TestParseFromHeader:
    def test_name_and_email(self):
        name, addr = _parse_from_header("Mario Rossi <mario@example.com>")
        assert name == "Mario Rossi"
        assert addr == "mario@example.com"

    def test_quoted_name(self):
        name, addr = _parse_from_header('"Rossi, Mario" <mario@example.com>')
        assert name == "Rossi, Mario"
        assert addr == "mario@example.com"

    def test_plain_email_only(self):
        name, addr = _parse_from_header("mario@example.com")
        assert name is None
        assert addr == "mario@example.com"

    def test_empty_string(self):
        name, addr = _parse_from_header("")
        assert name is None
        assert addr is None

    def test_lowercase_output(self):
        _, addr = _parse_from_header("Mario <MARIO@EXAMPLE.COM>")
        assert addr == "mario@example.com"


class TestExtractContact:
    def _make_message(self, from_val: str, subject: str = "Test") -> dict:
        return {
            "_parsed_id": "msg1",
            "payload": {
                "headers": [
                    {"name": "From", "value": from_val},
                    {"name": "Subject", "value": subject},
                ]
            },
        }

    def test_full_from_header(self):
        msg = self._make_message("Luca Bianchi <luca@bianchi.it>")
        contact = GmailClient.extract_contact(msg)
        assert contact is not None
        assert contact.email == "luca@bianchi.it"
        assert contact.first_name == "Luca"
        assert contact.last_name == "Bianchi"
        assert contact.company == "Bianchi"

    def test_no_from_header(self):
        msg = {"payload": {"headers": [{"name": "Subject", "value": "Hi"}]}}
        contact = GmailClient.extract_contact(msg)
        assert contact is None

    def test_subject_extraction(self):
        msg = self._make_message("x@y.com", "Ciao mondo")
        assert GmailClient.get_subject(msg) == "Ciao mondo"


class TestSyncEngineSystemFilter:
    def test_noreply_is_system(self):
        from gmail_hubspot_sync.sync_engine import SyncEngine
        assert SyncEngine._is_system_address("noreply@shopify.com")
        assert SyncEngine._is_system_address("no-reply@amazon.it")
        assert SyncEngine._is_system_address("mailer-daemon@google.com")

    def test_regular_address_not_system(self):
        from gmail_hubspot_sync.sync_engine import SyncEngine
        assert not SyncEngine._is_system_address("mario@example.com")
        assert not SyncEngine._is_system_address("info@company.com")
