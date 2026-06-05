import pytest
from src.gmail_to_hubspot.contact_parser import parse_from_header, ContactInfo


def test_full_name_corporate():
    c = parse_from_header("Mario Rossi <mario.rossi@acme.com>")
    assert c.email == "mario.rossi@acme.com"
    assert c.first_name == "Mario"
    assert c.last_name == "Rossi"
    assert c.company == "Acme"
    assert c.domain == "acme.com"


def test_bare_email():
    c = parse_from_header("info@startup.io")
    assert c.email == "info@startup.io"
    assert c.first_name == ""
    assert c.company == "Startup"


def test_gmail_free_domain_no_company():
    c = parse_from_header("Luigi Verdi <luigi@gmail.com>")
    assert c.company == ""


def test_quoted_display_name():
    c = parse_from_header('"Anna Bianchi" <anna@corp.it>')
    assert c.first_name == "Anna"
    assert c.last_name == "Bianchi"
    assert c.company == "Corp"


def test_empty_header_returns_none():
    assert parse_from_header("") is None
    assert parse_from_header("   ") is None


def test_single_word_name():
    c = parse_from_header("Support <support@helpdesk.com>")
    assert c.first_name == "Support"
    assert c.last_name == ""


def test_email_lowercased():
    c = parse_from_header("Test User <TEST@Company.COM>")
    assert c.email == "test@company.com"
