import pytest
from utils import company_from_domain, extract_email_parts, is_valid_email, parse_name


def test_extract_email_parts_with_name():
    email, name = extract_email_parts("John Doe <john.doe@acme.com>")
    assert email == "john.doe@acme.com"
    assert name == "John Doe"


def test_extract_email_parts_bare():
    email, name = extract_email_parts("jane@example.org")
    assert email == "jane@example.org"
    assert name is None


def test_extract_email_parts_quoted_name():
    email, name = extract_email_parts('"Alice Smith" <alice@corp.io>')
    assert email == "alice@corp.io"
    assert name == "Alice Smith"


def test_parse_name_full():
    first, last = parse_name("Mario Rossi")
    assert first == "Mario"
    assert last == "Rossi"


def test_parse_name_single():
    first, last = parse_name("Acme")
    assert first == "Acme"
    assert last is None


def test_parse_name_none():
    first, last = parse_name(None)
    assert first is None
    assert last is None


def test_company_from_domain():
    assert company_from_domain("acme.com") == "Acme"
    assert company_from_domain("bigcorp.io") == "Bigcorp"


def test_is_valid_email():
    assert is_valid_email("user@domain.com") is True
    assert is_valid_email("not-an-email") is False
    assert is_valid_email("@domain.com") is False
    assert is_valid_email("user@") is False
