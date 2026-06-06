"""Unit tests for Gmail sender parsing logic."""

import pytest
from gmail_hubspot_sync.gmail_client import parse_sender, company_from_domain

SKIP = {"gmail.com", "noreply.com"}


class TestParseSender:
    def test_full_name_and_email(self):
        result = parse_sender("Mario Rossi <mario.rossi@acme.com>")
        assert result["email"] == "mario.rossi@acme.com"
        assert result["first_name"] == "Mario"
        assert result["last_name"] == "Rossi"
        assert result["domain"] == "acme.com"

    def test_email_only(self):
        result = parse_sender("info@startup.io")
        assert result["email"] == "info@startup.io"
        assert result["first_name"] == ""
        assert result["domain"] == "startup.io"

    def test_quoted_display_name(self):
        result = parse_sender('"Anna Bianchi" <anna@corp.it>')
        assert result["first_name"] == "Anna"
        assert result["last_name"] == "Bianchi"

    def test_single_name(self):
        result = parse_sender("Support <support@help.com>")
        assert result["first_name"] == "Support"
        assert result["last_name"] == ""

    def test_invalid_email_returns_empty(self):
        result = parse_sender("not-an-email")
        assert result == {}

    def test_email_lowercased(self):
        result = parse_sender("User <USER@Example.COM>")
        assert result["email"] == "user@example.com"


class TestCompanyFromDomain:
    def test_standard_domain(self):
        assert company_from_domain("acme.com", SKIP) == "Acme"

    def test_subdomain_stripped(self):
        assert company_from_domain("mail.bigcorp.net", SKIP) == "Bigcorp"

    def test_skipped_domain_returns_none(self):
        assert company_from_domain("gmail.com", SKIP) is None

    def test_noreply_skipped(self):
        assert company_from_domain("noreply.com", SKIP) is None
