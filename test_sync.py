"""Unit tests for gmail_hubspot_sync.py — no MCP credentials required."""

import json
import pytest
from unittest.mock import MagicMock, patch, call
from pathlib import Path

from gmail_hubspot_sync import (
    SenderInfo,
    SyncResult,
    _company_from_email,
    _is_valid_email,
    find_contact_by_email,
    create_contact,
    update_contact,
    process_sender,
    extract_sender_from_thread,
    run_sync_cycle,
    STATE_FILE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestCompanyFromEmail:
    def test_corporate_domain(self):
        assert _company_from_email("user@acmecorp.com") == "Acmecorp"

    def test_personal_gmail(self):
        assert _company_from_email("user@gmail.com") == ""

    def test_personal_yahoo(self):
        assert _company_from_email("user@yahoo.com") == ""

    def test_startup_io(self):
        assert _company_from_email("user@startup.io") == "Startup"

    def test_invalid_no_at(self):
        assert _company_from_email("notanemail") == ""


class TestIsValidEmail:
    def test_valid(self):
        assert _is_valid_email("user@example.com")

    def test_missing_at(self):
        assert not _is_valid_email("userexample.com")

    def test_missing_tld(self):
        assert not _is_valid_email("user@example")

    def test_empty(self):
        assert not _is_valid_email("")


# ---------------------------------------------------------------------------
# SenderInfo parsing
# ---------------------------------------------------------------------------

class TestSenderInfo:
    def test_full_name_corporate(self):
        s = SenderInfo.from_from_header("Mario Rossi <mario@acme.it>")
        assert s.email == "mario@acme.it"
        assert s.first_name == "Mario"
        assert s.last_name == "Rossi"
        assert s.company == "Acme"

    def test_single_name(self):
        s = SenderInfo.from_from_header("Luigi <luigi@corp.com>")
        assert s.first_name == "Luigi"
        assert s.last_name == ""

    def test_no_display_name(self):
        s = SenderInfo.from_from_header("user@gmail.com")
        assert s.email == "user@gmail.com"
        assert s.first_name == ""
        assert s.company == ""

    def test_email_lowercased(self):
        s = SenderInfo.from_from_header("User@EXAMPLE.COM")
        assert s.email == "user@example.com"


# ---------------------------------------------------------------------------
# HubSpot interaction
# ---------------------------------------------------------------------------

def _make_hubspot(existing_contact=None):
    hs = MagicMock()
    if existing_contact:
        hs.search_crm_objects.return_value = {"results": [existing_contact]}
    else:
        hs.search_crm_objects.return_value = {"results": []}
    hs.manage_crm_objects.return_value = {"id": "hs_new_001"}
    return hs


class TestFindContact:
    def test_found(self):
        contact = {"id": "123", "properties": {"email": "a@b.com"}}
        hs = _make_hubspot(contact)
        result = find_contact_by_email(hs, "a@b.com")
        assert result == contact

    def test_not_found(self):
        hs = _make_hubspot()
        assert find_contact_by_email(hs, "missing@b.com") is None

    def test_exception_returns_none(self):
        hs = MagicMock()
        hs.search_crm_objects.side_effect = RuntimeError("network error")
        assert find_contact_by_email(hs, "x@y.com") is None


class TestCreateContact:
    def test_creates_with_all_fields(self):
        hs = _make_hubspot()
        sender = SenderInfo("user@corp.com", "Anna", "Verdi", "Corp")
        contact_id = create_contact(hs, sender)
        assert contact_id == "hs_new_001"
        hs.manage_crm_objects.assert_called_once()
        kwargs = hs.manage_crm_objects.call_args.kwargs
        assert kwargs["action"] == "create"
        assert kwargs["properties"]["email"] == "user@corp.com"
        assert kwargs["properties"]["hs_lead_source"] == "Gmail"


class TestUpdateContact:
    def test_fills_missing_fields_only(self):
        hs = MagicMock()
        existing = {
            "id": "99",
            "properties": {"firstname": "Existing", "lastname": "", "company": "", "hs_lead_source": ""},
        }
        sender = SenderInfo("u@corp.com", "NewFirst", "NewLast", "Corp")
        update_contact(hs, "99", existing, sender)
        kwargs = hs.manage_crm_objects.call_args.kwargs
        # firstname already set → must NOT be overwritten
        assert "firstname" not in kwargs["properties"]
        assert kwargs["properties"]["lastname"] == "NewLast"
        assert kwargs["properties"]["company"] == "Corp"

    def test_no_call_when_nothing_to_update(self):
        hs = MagicMock()
        existing = {
            "id": "99",
            "properties": {
                "firstname": "X", "lastname": "Y", "company": "Z", "hs_lead_source": "Gmail"
            },
        }
        sender = SenderInfo("u@z.com", "X", "Y", "Z")
        update_contact(hs, "99", existing, sender)
        hs.manage_crm_objects.assert_not_called()


# ---------------------------------------------------------------------------
# process_sender
# ---------------------------------------------------------------------------

class TestProcessSender:
    def test_creates_new_contact(self):
        hs = _make_hubspot()
        sender = SenderInfo("new@corp.io", "New", "User", "Corp")
        result = process_sender(hs, sender, add_activity=False)
        assert result.status == "created"
        assert result.contact_id == "hs_new_001"

    def test_updates_existing_contact(self):
        existing = {"id": "42", "properties": {"firstname": "", "lastname": "", "company": "", "hs_lead_source": ""}}
        hs = _make_hubspot(existing)
        sender = SenderInfo("existing@corp.io", "Old", "Name", "Corp")
        result = process_sender(hs, sender, add_activity=False)
        assert result.status == "updated"
        assert result.contact_id == "42"

    def test_skips_on_hubspot_error(self):
        hs = MagicMock()
        hs.search_crm_objects.return_value = {"results": []}
        hs.manage_crm_objects.side_effect = RuntimeError("API error")
        sender = SenderInfo("x@corp.io", "X", "", "Corp")
        result = process_sender(hs, sender, add_activity=False)
        assert result.status == "skipped"
        assert "API error" in result.reason


# ---------------------------------------------------------------------------
# Gmail thread extraction
# ---------------------------------------------------------------------------

def _make_gmail(thread_data: dict):
    g = MagicMock()
    g.get_thread.return_value = thread_data
    return g


class TestExtractSender:
    def test_extracts_from_header(self):
        thread = {
            "messages": [
                {"payload": {"headers": [{"name": "From", "value": "Luca Neri <luca@firm.it>"}]}}
            ]
        }
        gmail = _make_gmail(thread)
        sender = extract_sender_from_thread(gmail, "t1")
        assert sender is not None
        assert sender.email == "luca@firm.it"
        assert sender.first_name == "Luca"

    def test_returns_none_on_missing_from(self):
        thread = {"messages": [{"payload": {"headers": []}}]}
        gmail = _make_gmail(thread)
        assert extract_sender_from_thread(gmail, "t1") is None

    def test_returns_none_on_empty_thread(self):
        gmail = _make_gmail({"messages": []})
        assert extract_sender_from_thread(gmail, "t1") is None

    def test_returns_none_on_exception(self):
        gmail = MagicMock()
        gmail.get_thread.side_effect = RuntimeError("timeout")
        assert extract_sender_from_thread(gmail, "t1") is None


# ---------------------------------------------------------------------------
# run_sync_cycle — integration
# ---------------------------------------------------------------------------

class TestRunSyncCycle:
    def test_full_cycle_creates_two_contacts(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)

        gmail = MagicMock()
        gmail.search_threads.return_value = {
            "threads": [{"id": "ta"}, {"id": "tb"}]
        }
        gmail.get_thread.side_effect = [
            {
                "messages": [
                    {"payload": {"headers": [{"name": "From", "value": "Alice <alice@alpha.com>"}]}}
                ]
            },
            {
                "messages": [
                    {"payload": {"headers": [{"name": "From", "value": "Bob <bob@beta.com>"}]}}
                ]
            },
        ]

        hs = MagicMock()
        hs.search_crm_objects.return_value = {"results": []}
        id_counter = iter(["id_1", "id_2"])
        hs.manage_crm_objects.side_effect = lambda **kw: {"id": next(id_counter)}

        state = {"processed_ids": [], "last_run": None}
        results = run_sync_cycle(gmail, hs, state, add_activity=False)

        assert len(results) == 2
        assert all(r.status == "created" for r in results)
        assert {"alice@alpha.com", "bob@beta.com"} == {r.email for r in results}
        assert set(state["processed_ids"]) == {"ta", "tb"}

    def test_already_processed_threads_skipped(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        gmail = MagicMock()
        gmail.search_threads.return_value = {"threads": [{"id": "old_thread"}]}

        hs = MagicMock()
        state = {"processed_ids": ["old_thread"], "last_run": None}

        results = run_sync_cycle(gmail, hs, state, add_activity=False)
        assert results == []
        hs.search_crm_objects.assert_not_called()
