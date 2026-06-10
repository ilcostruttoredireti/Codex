"""Unit tests for the utils module — no external API calls required."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import (
    domain_to_company,
    extract_domain,
    is_ignorable,
    parse_sender,
    split_name,
)


# ── parse_sender ──────────────────────────────────────────────────────────────

def test_parse_sender_rfc():
    name, email = parse_sender('"Mario Rossi" <mario.rossi@example.com>')
    assert name == "Mario Rossi"
    assert email == "mario.rossi@example.com"


def test_parse_sender_bare_email():
    name, email = parse_sender("mario@example.com")
    assert name is None
    assert email == "mario@example.com"


def test_parse_sender_no_quotes():
    name, email = parse_sender("Mario Rossi <mario@example.com>")
    assert name == "Mario Rossi"
    assert email == "mario@example.com"


def test_parse_sender_lowercases_email():
    _, email = parse_sender("Test User <Test.User@Example.COM>")
    assert email == "test.user@example.com"


# ── split_name ────────────────────────────────────────────────────────────────

def test_split_name_first_last():
    assert split_name("Mario Rossi") == ("Mario", "Rossi")


def test_split_name_last_comma_first():
    assert split_name("Rossi, Mario") == ("Mario", "Rossi")


def test_split_name_dot_separated():
    first, last = split_name("mario.rossi")
    assert first.lower() == "mario"
    assert last.lower() == "rossi"


def test_split_name_single():
    assert split_name("Mario") == ("Mario", "")


def test_split_name_none():
    assert split_name(None) == ("", "")


# ── extract_domain ────────────────────────────────────────────────────────────

def test_extract_domain_normal():
    assert extract_domain("user@example.com") == "example.com"


def test_extract_domain_no_at():
    assert extract_domain("notanemail") == ""


# ── domain_to_company ─────────────────────────────────────────────────────────

def test_domain_to_company_business():
    assert domain_to_company("acmecorp.com") == "Acmecorp"


def test_domain_to_company_free_provider():
    assert domain_to_company("gmail.com") is None
    assert domain_to_company("yahoo.com") is None


def test_domain_to_company_hyphen():
    assert domain_to_company("my-company.it") == "My Company"


# ── is_ignorable ──────────────────────────────────────────────────────────────

IGNORED = {"gmail.com", "googlemail.com"}


def test_is_ignorable_noreply():
    assert is_ignorable("noreply@example.com", IGNORED) is True


def test_is_ignorable_ignored_domain():
    assert is_ignorable("user@gmail.com", IGNORED) is True


def test_is_ignorable_valid():
    assert is_ignorable("mario@acmecorp.com", IGNORED) is False


def test_is_ignorable_no_at():
    assert is_ignorable("notanemail", IGNORED) is True


def test_is_ignorable_mailer_daemon():
    assert is_ignorable("mailer-daemon@somehost.com", IGNORED) is True
