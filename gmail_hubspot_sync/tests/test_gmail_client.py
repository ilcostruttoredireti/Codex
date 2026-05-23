"""Unit tests for Gmail client helpers."""

import pytest
from gmail_client import _domain_to_company, _parse_name


@pytest.mark.parametrize("name,expected", [
    ("Mario Rossi", ("Mario", "Rossi")),
    ("Mario", ("Mario", "")),
    ("", ("", "")),
    ("Jean-Pierre Dupont", ("Jean-Pierre", "Dupont")),
    ("Alice B. Cooper", ("Alice", "B. Cooper")),
])
def test_parse_name(name, expected):
    assert _parse_name(name) == expected


@pytest.mark.parametrize("domain,expected", [
    ("gmail.com", ""),
    ("yahoo.com", ""),
    ("acme.com", "Acme"),
    ("mail.acmecorp.io", "Acmecorp"),
    ("startup.co", "Startup"),
])
def test_domain_to_company(domain, expected):
    assert _domain_to_company(domain) == expected
