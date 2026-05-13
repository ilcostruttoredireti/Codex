import pytest
from gmail_client import GmailClient
from models import SenderInfo


def _parse(header: str):
    return GmailClient.parse_sender(header)


class TestParseSender:
    def test_full_name_with_angle_brackets(self):
        s = _parse("Mario Rossi <mario.rossi@acme.com>")
        assert s.email == "mario.rossi@acme.com"
        assert s.first_name == "Mario"
        assert s.last_name == "Rossi"
        assert s.company == "Acme"
        assert s.domain == "acme.com"

    def test_quoted_name(self):
        s = _parse('"Giulia Bianchi" <giulia@startup.io>')
        assert s.email == "giulia@startup.io"
        assert s.first_name == "Giulia"
        assert s.last_name == "Bianchi"

    def test_email_only(self):
        s = _parse("someone@company.org")
        assert s.email == "someone@company.org"
        assert s.first_name is None
        assert s.last_name is None

    def test_single_word_name(self):
        s = _parse("Alice <alice@widgets.com>")
        assert s.first_name == "Alice"
        assert s.last_name is None

    def test_consumer_domain_no_company(self):
        s = _parse("User <user@gmail.com>")
        assert s.company is None

    def test_business_domain_infers_company(self):
        s = _parse("Bob <bob@techcorp.it>")
        assert s.company == "Techcorp"

    def test_noreply_returns_none(self):
        assert _parse("noreply@service.com") is None
        assert _parse("no-reply@newsletter.io") is None
        assert _parse("mailer-daemon@domain.com") is None
        assert _parse("bounce@email.io") is None

    def test_invalid_header_returns_none(self):
        assert _parse("not an email") is None

    def test_email_lowercased(self):
        s = _parse("User <USER@DOMAIN.COM>")
        assert s.email == "user@domain.com"
