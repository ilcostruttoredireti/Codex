"""
Unit tests for gmail_client helpers (no real API calls)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from gmail_client import _parse_from_header, _split_name, _company_from_domain


# ── _parse_from_header ────────────────────────────────────────

class TestParseFromHeader:
    def test_name_and_angle_addr(self):
        name, addr = _parse_from_header("Mario Rossi <mario@acme.it>")
        assert name == "Mario Rossi"
        assert addr == "mario@acme.it"

    def test_quoted_name(self):
        name, addr = _parse_from_header('"Acme Support" <support@acme.com>')
        assert name == "Acme Support"
        assert addr == "support@acme.com"

    def test_bare_email(self):
        name, addr = _parse_from_header("hello@world.org")
        assert name == ""
        assert addr == "hello@world.org"

    def test_no_display_name(self):
        name, addr = _parse_from_header("<info@company.com>")
        assert addr == "info@company.com"

    def test_extra_whitespace(self):
        name, addr = _parse_from_header("  John Doe  <john@doe.net>  ")
        assert addr == "john@doe.net"


# ── _split_name ───────────────────────────────────────────────

class TestSplitName:
    def test_first_last(self):
        assert _split_name("Mario Rossi") == ("Mario", "Rossi")

    def test_single_name(self):
        assert _split_name("Luigi") == ("Luigi", "")

    def test_empty(self):
        assert _split_name("") == ("", "")

    def test_multiple_parts(self):
        first, last = _split_name("Maria Grazia Curie")
        assert first == "Maria"
        assert last == "Grazia Curie"


# ── _company_from_domain ──────────────────────────────────────

class TestCompanyFromDomain:
    def test_personal_domain_returns_empty(self):
        assert _company_from_domain("gmail.com") == ""
        assert _company_from_domain("yahoo.com") == ""

    def test_corporate_domain(self):
        assert _company_from_domain("acme.com") == "Acme"

    def test_subdomain(self):
        # mail.bigcorp.it → Bigcorp
        result = _company_from_domain("mail.bigcorp.it")
        assert result == "Bigcorp"

    def test_country_tld(self):
        result = _company_from_domain("openai.com")
        assert result == "Openai"
