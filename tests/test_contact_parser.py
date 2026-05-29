"""Unit tests for contact_parser — no external dependencies required."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from gmail_hubspot_sync.contact_parser import ContactInfo, parse_sender


# ---------------------------------------------------------------------------
# Happy-path parsing
# ---------------------------------------------------------------------------

def test_full_name_with_angle_brackets():
    c = parse_sender("John Doe <john.doe@acme.com>")
    assert c.email == "john.doe@acme.com"
    assert c.firstname == "John"
    assert c.lastname == "Doe"
    assert not c.should_skip


def test_first_name_only():
    c = parse_sender("Alice <alice@startup.io>")
    assert c.firstname == "Alice"
    assert c.lastname is None
    assert c.email == "alice@startup.io"


def test_bare_email():
    c = parse_sender("contact@example.com")
    assert c.email == "contact@example.com"
    assert c.firstname is None
    assert c.lastname is None
    assert not c.should_skip


def test_email_normalised_to_lowercase():
    c = parse_sender("User@EXAMPLE.COM")
    assert c.email == "user@example.com"


# ---------------------------------------------------------------------------
# Company derivation
# ---------------------------------------------------------------------------

def test_company_from_standard_domain():
    c = parse_sender("info@latestata.it")
    assert c.company == "Latestata"


def test_company_from_subdomain():
    c = parse_sender("press@news.acme.com")
    assert c.company == "Acme"


def test_no_company_for_gmail():
    c = parse_sender("mario@gmail.com")
    assert c.company is None


def test_no_company_for_yahoo():
    c = parse_sender("user@yahoo.it")
    assert c.company is None


# ---------------------------------------------------------------------------
# Skip rules
# ---------------------------------------------------------------------------

def test_skip_noreply():
    c = parse_sender("noreply@example.com")
    assert c.should_skip
    assert c.skip_reason == "automated_sender"


def test_skip_no_dash_reply():
    c = parse_sender("no-reply@service.com")
    assert c.should_skip


def test_skip_notification():
    c = parse_sender("notification@priority.facebookmail.com")
    assert c.should_skip


def test_skip_facebookmail_domain():
    c = parse_sender("any@facebookmail.com")
    assert c.should_skip


def test_skip_bounce_prefix():
    c = parse_sender("bounce@mail.example.com")
    assert c.should_skip


def test_skip_bounces_subdomain():
    c = parse_sender("info@bounces.sendgrid.net")
    assert c.should_skip


def test_skip_mailer_daemon():
    c = parse_sender("mailer-daemon@mx.google.com")
    assert c.should_skip


def test_skip_postmaster():
    c = parse_sender("postmaster@example.com")
    assert c.should_skip


def test_skip_newsletter():
    c = parse_sender("newsletter@company.com")
    assert c.should_skip


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_from_header():
    c = parse_sender("")
    assert c.should_skip


def test_invalid_email():
    c = parse_sender("not-an-email")
    assert c.should_skip


def test_quoted_display_name():
    c = parse_sender('"Mario Rossi" <mario@esempio.it>')
    assert c.firstname == "Mario"
    assert c.lastname == "Rossi"
    assert c.email == "mario@esempio.it"


def test_plus_addressing_not_skipped():
    """user+tag@example.com should not be treated as noreply."""
    c = parse_sender("alice+work@acme.com")
    assert not c.should_skip
    assert c.email == "alice+work@acme.com"


def test_noreply_plus_subaddress_still_skipped():
    """noreply+something@example.com should still be skipped."""
    c = parse_sender("noreply+abc@example.com")
    assert c.should_skip
