"""Unit tests for gmail_hubspot_sync (no network calls)."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gmail_hubspot_sync import (
    HubSpotClient,
    SenderInfo,
    SyncResult,
    _parse_sender,
    process_email,
)


# ---------------------------------------------------------------------------
# _parse_sender
# ---------------------------------------------------------------------------

class TestParseSender:
    def test_full_name_and_address(self):
        s = _parse_sender("Mario Rossi <mario.rossi@acme.com>")
        assert s.email == "mario.rossi@acme.com"
        assert s.first_name == "Mario"
        assert s.last_name == "Rossi"
        assert s.company == "Acme"

    def test_address_only(self):
        s = _parse_sender("user@example.org")
        assert s.email == "user@example.org"
        assert s.first_name == ""
        assert s.company == "Example"

    def test_free_email_no_company(self):
        s = _parse_sender("user@gmail.com")
        assert s.company == ""

    def test_quoted_display_name(self):
        s = _parse_sender('"John Doe" <john@corp.io>')
        assert s.first_name == "John"
        assert s.last_name == "Doe"
        assert s.email == "john@corp.io"

    def test_single_name(self):
        s = _parse_sender("Alice <alice@startup.io>")
        assert s.first_name == "Alice"
        assert s.last_name == ""

    def test_email_is_lowercased(self):
        s = _parse_sender("User@Example.COM")
        assert s.email == "user@example.com"


# ---------------------------------------------------------------------------
# HubSpotClient._build_properties
# ---------------------------------------------------------------------------

class TestBuildProperties:
    def test_new_contact_all_fields(self):
        sender = SenderInfo(
            email="a@b.com", first_name="A", last_name="B", company="Corp"
        )
        props = HubSpotClient._build_properties(sender, is_new=True)
        assert props["email"] == "a@b.com"
        assert props["firstname"] == "A"
        assert props["lastname"] == "B"
        assert props["company"] == "Corp"
        assert props["hs_lead_source"] == "GMAIL"

    def test_update_skips_existing_fields(self):
        sender = SenderInfo(
            email="a@b.com", first_name="New", last_name="Name", company="NewCorp"
        )
        existing = {"firstname": "Old", "lastname": "Name", "company": ""}
        props = HubSpotClient._build_properties(sender, is_new=False, existing=existing)
        assert "firstname" not in props   # already set
        assert "lastname" not in props    # already set
        assert props.get("company") == "NewCorp"  # was empty → fill in

    def test_update_returns_empty_when_nothing_to_do(self):
        sender = SenderInfo(email="a@b.com", first_name="X", last_name="Y", company="Z")
        existing = {"firstname": "X", "lastname": "Y", "company": "Z"}
        props = HubSpotClient._build_properties(sender, is_new=False, existing=existing)
        assert props == {}


# ---------------------------------------------------------------------------
# process_email (integration-style, mocked HubSpot)
# ---------------------------------------------------------------------------

class TestProcessEmail:
    def _make_hs(self, existing=None, create_id="42", update_ok=True):
        hs = MagicMock(spec=HubSpotClient)
        hs.find_contact_by_email.return_value = existing
        hs.create_contact.return_value = create_id
        hs.update_contact.return_value = update_ok
        return hs

    def test_creates_new_contact(self):
        sender = SenderInfo(email="new@corp.com", first_name="New")
        hs = self._make_hs(existing=None, create_id="99")
        result = process_email("msg1", sender, hs, add_timeline=False)
        assert result.status == "created"
        assert result.contact_id == "99"
        hs.create_contact.assert_called_once_with(sender)

    def test_updates_existing_contact(self):
        sender = SenderInfo(email="old@corp.com", first_name="Old")
        existing = {"id": "7", "properties": {"firstname": "", "company": ""}}
        hs = self._make_hs(existing=existing, update_ok=True)
        result = process_email("msg2", sender, hs, add_timeline=False)
        assert result.status == "updated"
        assert result.contact_id == "7"

    def test_ignored_when_nothing_to_update(self):
        sender = SenderInfo(email="same@corp.com", first_name="Same", last_name="Guy")
        existing = {
            "id": "5",
            "properties": {"firstname": "Same", "lastname": "Guy", "company": "Corp"},
        }
        hs = self._make_hs(existing=existing, update_ok=False)
        result = process_email("msg3", sender, hs, add_timeline=False)
        assert result.status == "ignored"

    def test_error_on_create_failure(self):
        sender = SenderInfo(email="fail@corp.com")
        hs = self._make_hs(existing=None, create_id=None)
        result = process_email("msg4", sender, hs, add_timeline=False)
        assert result.status == "error"

    def test_timeline_activity_called_on_create(self):
        sender = SenderInfo(email="tl@corp.com")
        hs = self._make_hs(existing=None, create_id="10")
        process_email("msg5", sender, hs, add_timeline=True)
        hs.add_timeline_activity.assert_called_once_with("10", sender)

    def test_timeline_activity_called_on_update(self):
        sender = SenderInfo(email="tl2@corp.com", first_name="T")
        existing = {"id": "11", "properties": {"firstname": "", "company": ""}}
        hs = self._make_hs(existing=existing, update_ok=True)
        process_email("msg6", sender, hs, add_timeline=True)
        hs.add_timeline_activity.assert_called_once_with("11", sender)
