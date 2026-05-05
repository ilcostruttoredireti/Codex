"""Unit tests for sender parsing logic (no external dependencies)."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from contact_parser import parse_sender, extract_company_from_domain


def test_full_name():
    s = parse_sender("Mario Rossi <mario.rossi@acme.com>")
    assert s is not None
    assert s.email == "mario.rossi@acme.com"
    assert s.first_name == "Mario"
    assert s.last_name == "Rossi"
    assert s.company == "Acme"
    assert s.domain == "acme.com"


def test_email_only():
    s = parse_sender("info@example.io")
    assert s is not None
    assert s.email == "info@example.io"
    assert s.first_name == ""
    assert s.company == "Example"


def test_ignored_domain():
    s = parse_sender("no-reply <bot@noreply.github.com>")
    assert s is None


def test_noreply_prefix():
    s = parse_sender("noreply@someservice.com")
    assert s is None


def test_invalid_email():
    s = parse_sender("not an email at all")
    assert s is None


def test_company_from_domain():
    assert extract_company_from_domain("stripe.com") == "Stripe"
    assert extract_company_from_domain("my.company.co.uk") == "My"
    assert extract_company_from_domain("openai.io") == "Openai"


def test_metadata_preserved():
    s = parse_sender(
        "Luca Bianchi <luca@startup.it>",
        msg_id="abc123",
        subject="Ciao!",
        received_at="Mon, 5 May 2026 10:00:00 +0200",
    )
    assert s is not None
    assert s.message_id == "abc123"
    assert s.subject == "Ciao!"
    assert s.received_at == "Mon, 5 May 2026 10:00:00 +0200"
