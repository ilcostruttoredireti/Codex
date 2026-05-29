import pytest
from gmail_hubspot_sync.contact_extractor import extract_sender


def test_full_name_and_corporate_domain():
    sender = extract_sender("Mario Rossi <mario.rossi@acme.com>")
    assert sender.email == "mario.rossi@acme.com"
    assert sender.first_name == "Mario"
    assert sender.last_name == "Rossi"
    assert sender.company == "Acme"
    assert sender.domain == "acme.com"


def test_email_only():
    sender = extract_sender("info@example.com")
    assert sender.email == "info@example.com"
    assert sender.first_name is None
    assert sender.last_name is None


def test_single_display_name():
    sender = extract_sender("Giovanni <giovanni@startup.io>")
    assert sender.first_name == "Giovanni"
    assert sender.last_name is None


def test_last_comma_first_format():
    sender = extract_sender('"Bianchi, Luca" <l.bianchi@corp.it>')
    assert sender.last_name == "Bianchi"
    assert sender.first_name == "Luca"


def test_gmail_domain_no_company():
    sender = extract_sender("Test User <test@gmail.com>")
    assert sender.company is None


def test_corporate_domain_sets_company():
    sender = extract_sender("Someone <x@mycompany.net>")
    assert sender.company == "Mycompany"


def test_invalid_address_returns_none():
    assert extract_sender("not-an-email") is None


def test_empty_from_returns_none():
    assert extract_sender("") is None


def test_email_lowercased():
    sender = extract_sender("User <USER@EXAMPLE.COM>")
    assert sender.email == "user@example.com"


def test_hyphenated_domain_company():
    sender = extract_sender("a@my-great-company.com")
    assert sender.company == "My Great Company"
