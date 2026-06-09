import pytest
from gmail_hubspot_sync.gmail_client import GmailClient


@pytest.mark.parametrize("from_header,expected", [
    # Full name + email
    (
        '"Mario Rossi" <mario.rossi@acme.com>',
        {"email": "mario.rossi@acme.com", "first_name": "Mario", "last_name": "Rossi", "company": "Acme"},
    ),
    # Name without quotes
    (
        "Luca Bianchi <luca@example.it>",
        {"email": "luca@example.it", "first_name": "Luca", "last_name": "Bianchi", "company": "Example"},
    ),
    # Plain email only
    (
        "info@startup.io",
        {"email": "info@startup.io", "first_name": None, "last_name": None},
    ),
    # Gmail address — no company
    (
        "someone@gmail.com",
        {"email": "someone@gmail.com", "company": None},
    ),
    # Single-word name
    (
        "Alice <alice@beta.tech>",
        {"email": "alice@beta.tech", "first_name": "Alice", "last_name": None},
    ),
])
def test_parse_from_header(from_header, expected):
    contact = GmailClient.parse_from_header(from_header)
    assert contact is not None
    for key, val in expected.items():
        assert getattr(contact, key) == val, f"Field {key!r}: got {getattr(contact, key)!r}, expected {val!r}"


@pytest.mark.parametrize("from_header", [
    "no-reply@service.com",
    "noreply@notifications.com",
    "mailer-daemon@googlemail.com",
    "bounce@acme.com",
    "postmaster@example.com",
])
def test_ignored_senders(from_header):
    contact = GmailClient.parse_from_header(from_header)
    assert contact is None, f"Expected None for {from_header!r}, got {contact!r}"


def test_malformed_from_returns_none():
    assert GmailClient.parse_from_header("not an email at all") is None
    assert GmailClient.parse_from_header("") is None
