"""Unit tests for gmail_hubspot_sync (no MCP required)."""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from gmail_hubspot_sync import (
    parse_sender,
    SenderInfo,
    SyncResult,
    _hs_build_properties,
    IGNORED_DOMAINS,
)


# ---------------------------------------------------------------------------
# parse_sender
# ---------------------------------------------------------------------------

class TestParseSender:
    def test_full_name_and_email(self):
        info = parse_sender("Alice Smith <alice@example.com>")
        assert info is not None
        assert info.email == "alice@example.com"
        assert info.first_name == "Alice"
        assert info.last_name == "Smith"
        assert info.company == "Example"

    def test_email_only(self):
        info = parse_sender("bob@acme.io")
        assert info is not None
        assert info.email == "bob@acme.io"
        assert info.first_name == ""
        assert info.last_name == ""
        assert info.company == "Acme"

    def test_consumer_domain_no_company(self):
        info = parse_sender("user@gmail.com")
        assert info is not None
        assert info.company == ""

    def test_invalid_returns_none(self):
        assert parse_sender("not-an-email") is None
        assert parse_sender("") is None

    def test_case_normalised(self):
        info = parse_sender("USER@Example.COM")
        assert info.email == "user@example.com"

    def test_single_word_name(self):
        info = parse_sender("Carlos <carlos@startup.dev>")
        assert info.first_name == "Carlos"
        assert info.last_name == ""

    def test_hyphenated_domain(self):
        info = parse_sender("x@my-company.com")
        assert info.company == "My Company"


# ---------------------------------------------------------------------------
# SenderInfo.company_from_domain
# ---------------------------------------------------------------------------

class TestCompanyFromDomain:
    def test_ignored_domain(self):
        for d in IGNORED_DOMAINS:
            info = SenderInfo(email=f"user@{d}")
            assert info.company_from_domain == ""

    def test_subdomain_ignored(self):
        info = SenderInfo(email="x@mail.acme.co")
        # first segment is "mail" — not ideal but deterministic
        assert info.company_from_domain == "Mail"

    def test_normal_domain(self):
        info = SenderInfo(email="x@openai.com")
        assert info.company_from_domain == "Openai"


# ---------------------------------------------------------------------------
# _hs_build_properties
# ---------------------------------------------------------------------------

class TestBuildProperties:
    def _make_existing(self, **props) -> dict:
        return {"id": "123", "properties": props}

    def test_create_all_fields(self):
        info = SenderInfo(email="a@corp.com", first_name="Ana", last_name="B", company="Corp")
        props = _hs_build_properties(info)
        assert props["email"] == "a@corp.com"
        assert props["firstname"] == "Ana"
        assert props["lastname"] == "B"
        assert props["company"] == "Corp"
        assert props["hs_lead_source"] == "Gmail"

    def test_skip_existing_filled_fields(self):
        info = SenderInfo(email="a@corp.com", first_name="Ana", last_name="B", company="Corp")
        existing = self._make_existing(firstname="Ana", lastname="B", company="Corp")
        props = _hs_build_properties(info, existing=existing)
        assert "firstname" not in props
        assert "lastname" not in props
        assert "company" not in props

    def test_fill_missing_fields_on_existing(self):
        info = SenderInfo(email="a@corp.com", first_name="Ana", last_name="B", company="Corp")
        existing = self._make_existing(firstname="Ana")  # lastname & company missing
        props = _hs_build_properties(info, existing=existing)
        assert "firstname" not in props   # already set
        assert props["lastname"] == "B"
        assert props["company"] == "Corp"

    def test_empty_value_not_written(self):
        info = SenderInfo(email="a@gmail.com")  # no name, no company
        props = _hs_build_properties(info)
        assert "firstname" not in props
        assert "lastname" not in props
        assert "company" not in props


# ---------------------------------------------------------------------------
# SyncResult str representation
# ---------------------------------------------------------------------------

class TestSyncResult:
    def test_str_created(self):
        r = SyncResult("created", "x@y.com", contact_id="42")
        s = str(r)
        assert "CREATED" in s
        assert "x@y.com" in s
        assert "42" in s

    def test_str_ignored_no_id(self):
        r = SyncResult("ignored", "bot@y.com", detail="Automated sender")
        s = str(r)
        assert "IGNORED" in s
        assert "Automated sender" in s
        assert "ID" not in s
