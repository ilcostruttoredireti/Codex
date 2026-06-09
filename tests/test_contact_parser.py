import pytest
from gmail_hubspot_sync.contact_parser import parse_sender, _domain_to_company


class TestParseSender:
    def test_full_name_and_business_email(self):
        result = parse_sender("Mario Rossi <mario.rossi@acmecorp.com>")
        assert result["email"] == "mario.rossi@acmecorp.com"
        assert result["first_name"] == "Mario"
        assert result["last_name"] == "Rossi"
        assert result["domain"] == "acmecorp.com"
        assert result["company"] == "Acmecorp"

    def test_email_only(self):
        result = parse_sender("noreply@example.org")
        assert result["email"] == "noreply@example.org"
        assert result["first_name"] == ""
        assert result["last_name"] == ""

    def test_free_provider_has_no_company(self):
        result = parse_sender("Luca Bianchi <luca.bianchi@gmail.com>")
        assert result["company"] == ""

    def test_business_domain_extracts_company(self):
        result = parse_sender("Anna <anna@startup-io.it>")
        assert result["company"] == "Startup Io"

    def test_invalid_returns_none(self):
        assert parse_sender("not-an-email") is None

    def test_email_lowercased(self):
        result = parse_sender("Test <TEST@Example.COM>")
        assert result["email"] == "test@example.com"


class TestDomainToCompany:
    def test_strips_tld(self):
        assert _domain_to_company("hubspot.com") == "Hubspot"

    def test_free_provider_empty(self):
        assert _domain_to_company("gmail.com") == ""

    def test_hyphenated_domain(self):
        assert _domain_to_company("my-company.io") == "My Company"
