"""
Tests for _build_properties logic without triggering Google auth imports.
We replicate only the pure logic here, as gmail_client imports Google auth at module level.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import dataclass
from unittest.mock import MagicMock

from utils import domain_to_company, parse_name

CONTACT_SOURCE = "Gmail"


def build_properties(sender, existing) -> dict:
    """Mirror of hubspot_client._build_properties for isolated testing."""
    props: dict = {
        "email": sender.email,
        "hs_lead_source": CONTACT_SOURCE,
    }
    existing_props = existing.properties if existing else {}

    if not existing_props.get("firstname") and sender.first_name:
        props["firstname"] = sender.first_name
    if not existing_props.get("lastname") and sender.last_name:
        props["lastname"] = sender.last_name
    if not existing_props.get("company") and sender.company:
        props["company"] = sender.company

    return props


@dataclass
class FakeSender:
    email: str
    first_name: str
    last_name: str
    company: str


def test_new_contact_all_fields():
    s = FakeSender("mario@acme.com", "Mario", "Rossi", "Acme")
    props = build_properties(s, None)
    assert props == {
        "email": "mario@acme.com",
        "hs_lead_source": "Gmail",
        "firstname": "Mario",
        "lastname": "Rossi",
        "company": "Acme",
    }


def test_existing_contact_all_filled():
    existing = MagicMock()
    existing.properties = {"firstname": "Old", "lastname": "Name", "company": "OldCo"}
    s = FakeSender("mario@acme.com", "Mario", "Rossi", "Acme")
    props = build_properties(s, existing)
    assert "firstname" not in props
    assert "lastname" not in props
    assert "company" not in props


def test_existing_contact_partial():
    existing = MagicMock()
    existing.properties = {"firstname": "Mario", "lastname": "", "company": ""}
    s = FakeSender("mario@acme.com", "Mario", "Rossi", "Acme")
    props = build_properties(s, existing)
    assert "firstname" not in props
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"


def test_free_email_no_company():
    s = FakeSender("user@gmail.com", "User", "", "")
    props = build_properties(s, None)
    assert "company" not in props
    assert "lastname" not in props


def test_parse_and_domain_pipeline():
    display = "Luca Bianchi"
    email = "luca@techcorp.io"
    domain = email.split("@")[1]
    first, last = parse_name(display)
    company = domain_to_company(domain)
    assert first == "Luca"
    assert last == "Bianchi"
    assert company == "Techcorp"
