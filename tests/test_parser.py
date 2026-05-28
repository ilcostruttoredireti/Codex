"""Unit tests for the From-header parser."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from parser import parse_from_header


def test_full_name_and_email():
    c = parse_from_header("Mario Rossi <mario.rossi@acme.com>")
    assert c.email == "mario.rossi@acme.com"
    assert c.first_name == "Mario"
    assert c.last_name == "Rossi"
    assert c.company == "Acme"


def test_bare_email():
    c = parse_from_header("info@example.com")
    assert c.email == "info@example.com"
    assert c.first_name is None
    assert c.company == "Example"


def test_gmail_domain_no_company():
    c = parse_from_header("Luca Bianchi <luca@gmail.com>")
    assert c.company is None


def test_quoted_name():
    c = parse_from_header('"Anna Verdi" <anna@startup.io>')
    assert c.first_name == "Anna"
    assert c.last_name == "Verdi"
    assert c.email == "anna@startup.io"


def test_lowercase_normalisation():
    c = parse_from_header("Test User <TEST@Company.COM>")
    assert c.email == "test@company.com"


def test_single_name():
    c = parse_from_header("Gianni <gianni@firm.it>")
    assert c.first_name == "Gianni"
    assert c.last_name is None


def test_domain_property():
    c = parse_from_header("x@beta.org")
    assert c.domain == "beta.org"
