"""Unit test per il parser dei contatti email."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gmail_hubspot_sync.contact_parser import parse_sender, is_system_sender


def test_parse_full_name_and_company():
    c = parse_sender("Mario Rossi <mario.rossi@acme.com>")
    assert c is not None
    assert c.email == "mario.rossi@acme.com"
    assert c.first_name == "Mario"
    assert c.last_name == "Rossi"
    assert c.company == "Acme"


def test_parse_gmail_no_company():
    c = parse_sender("Luca Bianchi <luca@gmail.com>")
    assert c is not None
    assert c.company is None


def test_parse_no_display_name():
    c = parse_sender("info@startup.io")
    assert c is not None
    assert c.first_name is None
    assert c.company == "Startup"


def test_is_system_sender_noreply():
    assert is_system_sender("noreply@shopify.com") is True
    assert is_system_sender("no-reply@amazon.com") is True
    assert is_system_sender("newsletter@example.com") is True


def test_is_system_sender_real_person():
    assert is_system_sender("mario@azienda.it") is False
    assert is_system_sender("john.doe@company.com") is False


def test_invalid_email():
    assert parse_sender("not-an-email") is None
    assert parse_sender("") is None


if __name__ == "__main__":
    tests = [
        test_parse_full_name_and_company,
        test_parse_gmail_no_company,
        test_parse_no_display_name,
        test_is_system_sender_noreply,
        test_is_system_sender_real_person,
        test_invalid_email,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
    print(f"\n{passed}/{len(tests)} test superati")
