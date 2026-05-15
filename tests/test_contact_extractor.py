import pytest
from gmail_hubspot_sync.contact_extractor import extract_contact


def test_full_display_name():
    c = extract_contact("Mario Rossi <mario.rossi@acme.com>")
    assert c["email"] == "mario.rossi@acme.com"
    assert c["first_name"] == "Mario"
    assert c["last_name"] == "Rossi"
    assert c["company"] == "Acme"
    assert c["domain"] == "acme.com"


def test_no_display_name_dotted_local():
    c = extract_contact("john.doe@company.io")
    assert c["first_name"] == "John"
    assert c["last_name"] == "Doe"
    assert c["company"] == "Company"


def test_personal_domain_no_company():
    c = extract_contact("Alice <alice@gmail.com>")
    assert c["company"] is None


def test_noreply_email():
    c = extract_contact("no-reply@service.com")
    assert c["email"] == "no-reply@service.com"


def test_invalid_header_returns_empty():
    c = extract_contact("not an email at all")
    assert c == {}


def test_email_only_no_at_sign():
    c = extract_contact("plainstring")
    assert c == {}


def test_plus_tag_stripped():
    c = extract_contact("user+tag@example.com")
    assert c["email"] == "user+tag@example.com"
    assert c["first_name"] == "User"


def test_single_word_display_name():
    c = extract_contact("Boss <boss@corp.net>")
    assert c["first_name"] == "Boss"
    assert c["last_name"] == ""
