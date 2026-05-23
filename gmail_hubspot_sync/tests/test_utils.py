"""Unit tests for pure utility helpers."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from utils import domain_to_company, extract_email_parts, parse_name


@pytest.mark.parametrize("name,expected", [
    ("Mario Rossi", ("Mario", "Rossi")),
    ("Mario", ("Mario", "")),
    ("", ("", "")),
    ("Jean-Pierre Dupont", ("Jean-Pierre", "Dupont")),
    ("Alice B. Cooper", ("Alice", "B. Cooper")),
])
def test_parse_name(name, expected):
    assert parse_name(name) == expected


@pytest.mark.parametrize("domain,expected", [
    ("gmail.com", ""),
    ("yahoo.com", ""),
    ("acme.com", "Acme"),
    ("mail.acmecorp.io", "Acmecorp"),
    ("startup.co", "Startup"),
    ("mycompany.it", "Mycompany"),
])
def test_domain_to_company(domain, expected):
    assert domain_to_company(domain) == expected


@pytest.mark.parametrize("email,expected", [
    ("user@example.com", ("user", "example.com")),
    ("MARIO@ACME.COM", ("mario", "acme.com")),
])
def test_extract_email_parts(email, expected):
    assert extract_email_parts(email) == expected
