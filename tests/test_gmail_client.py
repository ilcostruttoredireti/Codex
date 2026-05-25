"""Test unitari per gmail_client.py (parsing, filtri)."""

import os
import sys
import pytest

# Imposta variabili ambiente minime prima di importare il modulo
os.environ.setdefault("HUBSPOT_ACCESS_TOKEN", "test-token-dummy")

from gmail_hubspot_sync.gmail_client import (
    _parse_from_header,
    _parse_name,
    SenderInfo,
)
from gmail_hubspot_sync import config


# ── _parse_from_header ────────────────────────────────────────────────────────

class TestParseFromHeader:
    def test_full_format(self):
        name, addr = _parse_from_header("Mario Rossi <mario@example.com>")
        assert name == "Mario Rossi"
        assert addr == "mario@example.com"

    def test_email_only(self):
        name, addr = _parse_from_header("mario@example.com")
        assert name == ""
        assert addr == "mario@example.com"

    def test_angle_brackets_only(self):
        name, addr = _parse_from_header("<mario@example.com>")
        assert name == ""
        assert addr == "mario@example.com"

    def test_uppercase_email(self):
        _, addr = _parse_from_header("Mario <MARIO@EXAMPLE.COM>")
        assert addr == "mario@example.com"

    def test_quoted_name(self):
        name, addr = _parse_from_header('"Acme Corp" <info@acme.com>')
        assert "Acme" in name
        assert addr == "info@acme.com"

    def test_empty_string(self):
        name, addr = _parse_from_header("")
        assert addr == ""


# ── _parse_name ───────────────────────────────────────────────────────────────

class TestParseName:
    def test_first_last(self):
        first, last = _parse_name("Mario Rossi")
        assert first == "Mario"
        assert last == "Rossi"

    def test_last_comma_first(self):
        first, last = _parse_name("Rossi, Mario")
        assert first == "Mario"
        assert last == "Rossi"

    def test_single_name(self):
        first, last = _parse_name("Mario")
        assert first == "Mario"
        assert last == ""

    def test_compound_last_name(self):
        first, last = _parse_name("Mario De Rossi")
        assert first == "Mario"
        assert last == "De Rossi"

    def test_empty(self):
        first, last = _parse_name("")
        assert first == ""
        assert last == ""


# ── SenderInfo.domain ─────────────────────────────────────────────────────────

class TestSenderInfo:
    def test_domain_from_email(self):
        s = SenderInfo(raw_from="a@example.com", email="a@example.com")
        assert s.domain == "example.com"

    def test_domain_not_overridden_if_set(self):
        s = SenderInfo(
            raw_from="a@example.com",
            email="a@example.com",
            domain="custom.domain",
        )
        assert s.domain == "custom.domain"


# ── Filtro _should_ignore ─────────────────────────────────────────────────────

class TestShouldIgnore:
    """Testa la logica di filtraggio tramite GmailClient._should_ignore."""

    def setup_method(self):
        from unittest.mock import patch
        # Evitiamo di autenticarci in test
        with patch.object(
            __builtins__, "__import__", side_effect=ImportError
        ) if False else __import__("contextlib").nullcontext():
            from gmail_hubspot_sync.gmail_client import GmailClient
            self.client = GmailClient.__new__(GmailClient)
            self.client._service = None
            self.client._creds = None
            import json, pathlib
            self.client._state_path = pathlib.Path(".test_state.json")
            self.client._state = {}

    def _make_sender(self, email: str) -> SenderInfo:
        return SenderInfo(raw_from=email, email=email)

    def test_noreply_ignored(self):
        s = self._make_sender("noreply@example.com")
        assert self.client._should_ignore(s, None) is True

    def test_no_reply_hyphen_ignored(self):
        s = self._make_sender("no-reply@newsletter.com")
        assert self.client._should_ignore(s, None) is True

    def test_system_domain_ignored(self):
        s = self._make_sender("user@mailer-daemon.google.com")
        assert self.client._should_ignore(s, None) is True

    def test_normal_sender_not_ignored(self):
        s = self._make_sender("mario@acme.com")
        assert self.client._should_ignore(s, "me@mine.com") is False

    def test_self_ignored_when_configured(self):
        original = config.IGNORE_SELF
        config.IGNORE_SELF = True
        s = self._make_sender("me@mine.com")
        result = self.client._should_ignore(s, "me@mine.com")
        config.IGNORE_SELF = original
        assert result is True
