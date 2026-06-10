"""Unit tests for contact_parser.py — no external API calls needed."""

import pytest
from contact_parser import parse_sender, _domain_to_company, _split_display_name


class TestParseSender:
    def test_full_name_and_business_domain(self):
        c = parse_sender("Mario Rossi <mario.rossi@acme.com>")
        assert c is not None
        assert c.email == "mario.rossi@acme.com"
        assert c.first_name == "Mario"
        assert c.last_name == "Rossi"
        assert c.company == "Acme"

    def test_single_name(self):
        c = parse_sender("Giulia <giulia@startup.io>")
        assert c is not None
        assert c.first_name == "Giulia"
        assert c.last_name is None

    def test_no_display_name(self):
        c = parse_sender("unknown@company.net")
        assert c is not None
        assert c.email == "unknown@company.net"
        assert c.first_name is None

    def test_personal_domain_gives_no_company(self):
        c = parse_sender("Luca <luca@gmail.com>")
        assert c is not None
        assert c.company is None

    def test_noreply_skipped(self):
        assert parse_sender("noreply@service.com") is None

    def test_no_reply_with_display_skipped(self):
        assert parse_sender("Service <no-reply@service.com>") is None

    def test_newsletter_skipped(self):
        assert parse_sender("newsletter@news.example.com") is None

    def test_malformed_email_returns_none(self):
        assert parse_sender("not-an-email") is None
        assert parse_sender("missing-at-sign.com") is None

    def test_email_lowercased(self):
        c = parse_sender("User <User@Domain.COM>")
        assert c is not None
        assert c.email == "user@domain.com"

    def test_quoted_display_name_stripped(self):
        c = parse_sender('"John Doe" <john@corp.io>')
        assert c is not None
        assert c.first_name == "John"
        assert c.last_name == "Doe"

    def test_hyphenated_domain_to_company(self):
        c = parse_sender("Alice <alice@my-company.com>")
        assert c is not None
        assert c.company == "My Company"

    def test_multipart_last_name(self):
        c = parse_sender("Anna Maria Bianchi <a@firm.com>")
        assert c is not None
        assert c.first_name == "Anna"
        assert c.last_name == "Maria Bianchi"


class TestDomainToCompany:
    def test_personal_domains_return_none(self):
        for domain in ("gmail.com", "hotmail.com", "libero.it", "icloud.com"):
            assert _domain_to_company(domain) is None

    def test_business_domain_title_cased(self):
        assert _domain_to_company("openai.com") == "Openai"
        assert _domain_to_company("anthropic.com") == "Anthropic"

    def test_hyphen_replaced_with_space(self):
        assert _domain_to_company("my-firm.co.uk") == "My Firm"


class TestSplitDisplayName:
    def test_empty(self):
        assert _split_display_name("") == (None, None)

    def test_one_word(self):
        assert _split_display_name("Marco") == ("Marco", None)

    def test_two_words(self):
        assert _split_display_name("Marco Polo") == ("Marco", "Polo")

    def test_three_words(self):
        first, last = _split_display_name("Maria De Luca")
        assert first == "Maria"
        assert last == "De Luca"
