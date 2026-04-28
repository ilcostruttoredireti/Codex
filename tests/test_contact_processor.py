"""Unit tests for contact_processor.py"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from contact_processor import _split_name, _domain_to_company, _extract_domain, extract_contact


def test_split_name_simple():
    assert _split_name("Mario Rossi") == ("Mario", "Rossi")


def test_split_name_comma():
    assert _split_name("Rossi, Mario") == ("Mario", "Rossi")


def test_split_name_single():
    assert _split_name("Mario") == ("Mario", "")


def test_split_name_empty():
    assert _split_name("") == ("", "")


def test_extract_domain():
    assert _extract_domain("mario@example.com") == "example.com"
    assert _extract_domain("no-at-sign") == ""


def test_domain_to_company_ignored():
    assert _domain_to_company("gmail.com") == ""
    assert _domain_to_company("hotmail.com") == ""


def test_domain_to_company_business():
    assert _domain_to_company("acme.com") == "Acme"
    assert _domain_to_company("mycompany.it") == "Mycompany"


def test_extract_contact_noreply_skipped():
    msg = {"from_email": "noreply@example.com", "from_name": ""}
    assert extract_contact(msg) is None


def test_extract_contact_empty_email():
    msg = {"from_email": "", "from_name": "Mario Rossi"}
    assert extract_contact(msg) is None


def test_extract_contact_full():
    msg = {
        "from_email": "mario.rossi@acme.com",
        "from_name": "Mario Rossi",
        "subject": "Hello",
        "snippet": "Hi there",
        "id": "abc123",
    }
    contact = extract_contact(msg)
    assert contact is not None
    assert contact.email == "mario.rossi@acme.com"
    assert contact.first_name == "Mario"
    assert contact.last_name == "Rossi"
    assert contact.company == "Acme"
    assert contact.source == "Gmail"
    assert "Inbound Gmail" in contact.tags


def test_extract_contact_gmail_no_company():
    msg = {
        "from_email": "user@gmail.com",
        "from_name": "User",
        "id": "x",
    }
    contact = extract_contact(msg)
    assert contact is not None
    assert contact.company == ""
