"""Unit tests for contact_extractor — no external dependencies needed."""
import pytest
from sync.contact_extractor import parse_sender, _company_from_domain


class TestParseSender:
    def test_full_name_and_domain(self):
        s = parse_sender("Mario Rossi <mario@acme.com>")
        assert s.email == "mario@acme.com"
        assert s.firstname == "Mario"
        assert s.lastname == "Rossi"
        assert s.company == "Acme"
        assert s.domain == "acme.com"

    def test_single_display_name(self):
        s = parse_sender("Mario <mario@acme.com>")
        assert s.firstname == "Mario"
        assert s.lastname is None

    def test_bare_email(self):
        s = parse_sender("mario@acme.com")
        assert s.email == "mario@acme.com"
        assert s.firstname == "Mario"  # inferred from local part
        assert s.lastname is None

    def test_noreply_still_parsed(self):
        s = parse_sender("noreply@acme.com")
        assert s is not None
        assert s.email == "noreply@acme.com"

    def test_email_normalised_to_lowercase(self):
        s = parse_sender("Mario Rossi <Mario@Acme.COM>")
        assert s.email == "mario@acme.com"

    def test_invalid_header_returns_none(self):
        assert parse_sender("") is None
        assert parse_sender("not-an-email") is None

    def test_quoted_display_name(self):
        s = parse_sender('"Luca Bianchi" <luca@corp.it>')
        assert s.firstname == "Luca"
        assert s.lastname == "Bianchi"

    def test_multi_word_last_name(self):
        s = parse_sender("De Luca Giovanni <g@startup.io>")
        assert s.firstname == "De"
        assert s.lastname == "Luca Giovanni"


class TestCompanyFromDomain:
    def test_generic_gmail_returns_none(self):
        assert _company_from_domain("gmail.com") is None

    def test_simple_domain(self):
        assert _company_from_domain("acme.com") == "Acme"

    def test_hyphenated_domain(self):
        assert _company_from_domain("acme-corp.com") == "Acme Corp"

    def test_subdomain_stripped(self):
        assert _company_from_domain("mail.acme.com") == "Acme"

    def test_country_tld_co_uk(self):
        # e.g. user@acme.co.uk → Acme
        assert _company_from_domain("acme.co.uk") == "Acme"

    def test_italian_tld(self):
        assert _company_from_domain("startup.it") == "Startup"
