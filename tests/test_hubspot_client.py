"""Unit tests for hubspot_client helpers."""

from gmail_hubspot_sync.hubspot_client import _company_from_domain, _build_properties


def test_company_from_domain_generic_returns_empty():
    assert _company_from_domain("gmail.com") == ""
    assert _company_from_domain("yahoo.com") == ""
    assert _company_from_domain("hotmail.com") == ""


def test_company_from_domain_business():
    assert _company_from_domain("acme.io") == "Acme"
    assert _company_from_domain("stripe.com") == "Stripe"


def test_build_properties_full():
    sender = {
        "email": "mario@acme.io",
        "first_name": "Mario",
        "last_name": "Rossi",
        "domain": "acme.io",
    }
    props = _build_properties(sender)
    assert props["email"] == "mario@acme.io"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"
    assert props["leadsource"] == "Gmail"


def test_build_properties_generic_email_no_company():
    sender = {
        "email": "mario@gmail.com",
        "first_name": "Mario",
        "last_name": "",
        "domain": "gmail.com",
    }
    props = _build_properties(sender)
    assert "company" not in props
    assert "lastname" not in props
