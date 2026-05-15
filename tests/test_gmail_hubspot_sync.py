"""Unit tests for gmail_hubspot_sync core logic (no network calls)."""

import pytest
from gmail_hubspot_sync import (
    _extract_sender,
    _build_properties,
    _company_from_domain,
    _parse_name,
    SyncResult,
)


# ── _parse_name ───────────────────────────────────────────────────────────────

def test_parse_name_full():
    assert _parse_name("Mario Rossi") == ("Mario", "Rossi")


def test_parse_name_single():
    assert _parse_name("Mario") == ("Mario", "")


def test_parse_name_empty():
    assert _parse_name("") == ("", "")


# ── _company_from_domain ─────────────────────────────────────────────────────

def test_company_from_domain():
    assert _company_from_domain("acme.it") == "Acme"
    assert _company_from_domain("microsoft.com") == "Microsoft"


# ── _extract_sender ───────────────────────────────────────────────────────────

def test_extract_full_header():
    s = _extract_sender("Mario Rossi <mario@acme.it>")
    assert s is not None
    assert s.email == "mario@acme.it"
    assert s.first_name == "Mario"
    assert s.last_name == "Rossi"
    assert s.company == "Acme"


def test_extract_email_only():
    s = _extract_sender("mario@bigcorp.com")
    assert s is not None
    assert s.email == "mario@bigcorp.com"
    assert s.first_name == ""
    assert s.last_name == ""


def test_skip_noreply():
    assert _extract_sender("noreply@service.com") is None
    assert _extract_sender("no-reply@updates.io") is None
    assert _extract_sender("notifications@app.net") is None


def test_skip_gmail_domain():
    assert _extract_sender("user@gmail.com") is None


def test_skip_yahoo():
    assert _extract_sender("user@yahoo.com") is None


def test_email_lowercased():
    s = _extract_sender("Alice <ALICE@Acme.IT>")
    assert s is not None
    assert s.email == "alice@acme.it"


def test_invalid_header_returns_none():
    assert _extract_sender("not-an-email") is None


# ── _build_properties ─────────────────────────────────────────────────────────

def test_build_creates_all_fields():
    from gmail_hubspot_sync import EmailSender
    sender = EmailSender(
        email="mario@acme.it",
        first_name="Mario",
        last_name="Rossi",
        company="Acme",
    )
    props = _build_properties(sender, existing=None)
    assert props["email"] == "mario@acme.it"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"
    assert props["leadsource"] == "Gmail"
    assert "Inbound Gmail" in props["hs_tag"]


def test_build_does_not_overwrite_existing_name():
    from gmail_hubspot_sync import EmailSender
    sender = EmailSender(email="mario@acme.it", first_name="Mario", last_name="Rossi", company="Acme")
    existing = {"id": "1", "properties": {"firstname": "Existing", "email": "mario@acme.it"}}
    props = _build_properties(sender, existing=existing)
    assert "firstname" not in props  # already set, should not overwrite


def test_build_appends_tag():
    from gmail_hubspot_sync import EmailSender
    sender = EmailSender(email="a@b.com", first_name="", last_name="", company="B")
    existing = {"id": "1", "properties": {"hs_tag": "OldTag", "email": "a@b.com"}}
    props = _build_properties(sender, existing=existing)
    assert "Inbound Gmail" in props["hs_tag"]
    assert "OldTag" in props["hs_tag"]


def test_build_no_duplicate_tag():
    from gmail_hubspot_sync import EmailSender
    sender = EmailSender(email="a@b.com", first_name="", last_name="", company="B")
    existing = {"id": "1", "properties": {"hs_tag": "Inbound Gmail", "email": "a@b.com"}}
    props = _build_properties(sender, existing=existing)
    assert "hs_tag" not in props  # tag already present, nothing to add


# ── SyncResult ────────────────────────────────────────────────────────────────

def test_sync_result_str_created():
    r = SyncResult("Creato", "mario@acme.it", "hs-1")
    assert "Creato" in str(r)
    assert "mario@acme.it" in str(r)
    assert "hs-1" in str(r)


def test_sync_result_str_no_id():
    r = SyncResult("Ignorato", "x@y.com", None)
    assert "N/A" in str(r)
