import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock
from contact_parser import build_hubspot_props, merge_props, _split_name, _domain_to_company
from models import SenderInfo


def _make_sender(raw_from: str, subject: str = "Hello") -> SenderInfo:
    return SenderInfo(raw_from, subject, "msg-001")


# ── _split_name ───────────────────────────────────────────────────────────────

def test_split_name_full():
    assert _split_name("Mario Rossi") == ("Mario", "Rossi")

def test_split_name_single():
    assert _split_name("Mario") == ("Mario", "")

def test_split_name_compound_last():
    assert _split_name("Anna Maria Bianchi") == ("Anna", "Maria Bianchi")

def test_split_name_empty():
    assert _split_name("") == ("", "")


# ── _domain_to_company ────────────────────────────────────────────────────────

def test_domain_to_company_business():
    assert _domain_to_company("acme.com") == "Acme"

def test_domain_to_company_personal_gmail():
    assert _domain_to_company("gmail.com") is None

def test_domain_to_company_personal_yahoo():
    assert _domain_to_company("yahoo.it") is None

def test_domain_to_company_io_tld():
    assert _domain_to_company("stripe.io") == "Stripe"

def test_domain_to_company_co_uk():
    # 'acme.co.uk' → strips '.co.uk', capitalise 'acme'
    result = _domain_to_company("acme.co.uk")
    assert result == "Acme"


# ── build_hubspot_props ───────────────────────────────────────────────────────

def test_build_props_full_name_business():
    sender = _make_sender("Mario Rossi <mario@acme.com>")
    props = build_hubspot_props(sender, "Gmail", "Inbound Gmail")
    assert props["email"] == "mario@acme.com"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"
    assert props["leadsource"] == "Gmail"

def test_build_props_no_name():
    sender = _make_sender("noreply@acme.com")
    props = build_hubspot_props(sender, "Gmail", "Inbound Gmail")
    assert "firstname" not in props
    assert "lastname" not in props
    assert props["company"] == "Acme"

def test_build_props_personal_email_no_company():
    sender = _make_sender("Mario <mario@gmail.com>")
    props = build_hubspot_props(sender, "Gmail", "Inbound Gmail")
    assert "company" not in props


# ── merge_props ───────────────────────────────────────────────────────────────

def test_merge_fills_missing():
    existing = {"firstname": "", "lastname": "", "company": "Acme"}
    new_props = {"email": "a@b.com", "firstname": "Mario", "lastname": "Rossi", "company": "OtherCo"}
    patch = merge_props(existing, new_props)
    assert patch["firstname"] == "Mario"
    assert patch["lastname"] == "Rossi"
    assert "company" not in patch   # already present
    assert "email" not in patch     # never patched

def test_merge_nothing_to_update():
    existing = {"firstname": "Mario", "lastname": "Rossi", "company": "Acme"}
    new_props = {"email": "a@b.com", "firstname": "Luigi", "company": "Other"}
    patch = merge_props(existing, new_props)
    assert patch == {}
