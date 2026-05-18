"""Unit tests for Gmail sender parsing helpers."""

import pytest
from gmail_hubspot_sync.gmail_client import _parse_name, _company_from_domain  # noqa: PLC2701


@pytest.mark.parametrize("display,expected", [
    ("John Doe", ("John", "Doe")),
    ("John", ("John", None)),
    ("", (None, None)),
    ("  ", (None, None)),
    ("Maria Rossi Verdi", ("Maria", "Rossi Verdi")),
])
def test_parse_name(display, expected):
    assert _parse_name(display) == expected


@pytest.mark.parametrize("domain,expected", [
    ("gmail.com", None),
    ("yahoo.it", None),
    ("acme.com", "Acme"),
    ("my-corp.co.uk", "My Corp"),  # .uk TLD stripped, hyphen → space
    ("startup.io", "Startup"),
])
def test_company_from_domain(domain, expected):
    assert _company_from_domain(domain) == expected
