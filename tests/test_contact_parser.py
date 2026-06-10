import pytest
from gmail_hubspot_sync.contact_parser import parse_from_header, ContactData


def test_full_name_and_corporate_email():
    result = parse_from_header("Mario Rossi <mario.rossi@acme.it>")
    assert result is not None
    assert result.email == "mario.rossi@acme.it"
    assert result.first_name == "Mario"
    assert result.last_name == "Rossi"
    assert result.company == "Acme"
    assert result.domain == "acme.it"


def test_personal_email_no_company():
    result = parse_from_header("Giulia Bianchi <giulia@gmail.com>")
    assert result is not None
    assert result.email == "giulia@gmail.com"
    assert result.company is None


def test_email_only_no_display_name():
    result = parse_from_header("info@startup.com")
    assert result is not None
    assert result.email == "info@startup.com"
    assert result.first_name is None
    assert result.last_name is None
    assert result.company == "Startup"


def test_email_with_hyphenated_domain():
    result = parse_from_header("contact@my-company.com")
    assert result is not None
    assert result.company == "My Company"


def test_empty_from_header():
    assert parse_from_header("") is None


def test_invalid_from_header():
    assert parse_from_header("not-an-email") is None


def test_email_normalization():
    result = parse_from_header("TEST@EXAMPLE.COM")
    assert result is not None
    assert result.email == "test@example.com"


def test_quoted_display_name():
    result = parse_from_header('"John Smith" <john@corp.com>')
    assert result is not None
    assert result.first_name == "John"
    assert result.last_name == "Smith"


def test_libero_it_is_personal():
    result = parse_from_header("Luca <luca@libero.it>")
    assert result is not None
    assert result.company is None


def test_single_name():
    result = parse_from_header("Amazon <no-reply@amazon.it>")
    assert result is not None
    assert result.first_name == "Amazon"
    assert result.last_name is None
