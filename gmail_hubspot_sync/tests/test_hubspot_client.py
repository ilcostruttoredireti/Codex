"""
Unit tests for hubspot_client helpers (no real API calls)
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from hubspot_client import _build_properties, _build_update_properties
from models import SenderInfo


def _make_sender(**kwargs) -> SenderInfo:
    defaults = dict(
        email="mario@acme.it",
        first_name="Mario",
        last_name="Rossi",
        full_name="Mario Rossi",
        company="Acme",
        domain="acme.it",
    )
    defaults.update(kwargs)
    return SenderInfo(**defaults)


class TestBuildProperties:
    def test_all_fields_present(self):
        sender = _make_sender()
        props = _build_properties(sender)
        assert props["email"] == "mario@acme.it"
        assert props["firstname"] == "Mario"
        assert props["lastname"] == "Rossi"
        assert props["company"] == "Acme"
        assert props["leadsource"] == "Gmail"

    def test_no_name(self):
        sender = _make_sender(first_name="", last_name="", full_name="")
        props = _build_properties(sender)
        assert "firstname" not in props
        assert "lastname" not in props

    def test_no_company(self):
        sender = _make_sender(company="")
        props = _build_properties(sender)
        assert "company" not in props


class TestBuildUpdateProperties:
    def test_fills_missing_fields(self):
        sender = _make_sender()
        existing = {"firstname": "", "lastname": "", "company": "", "leadsource": ""}
        updates = _build_update_properties(sender, existing)
        assert updates["firstname"] == "Mario"
        assert updates["lastname"] == "Rossi"
        assert updates["company"] == "Acme"
        assert updates["leadsource"] == "Gmail"

    def test_does_not_overwrite_existing(self):
        sender = _make_sender(first_name="New", last_name="Name")
        existing = {"firstname": "OldFirst", "lastname": "OldLast", "company": "OldCo"}
        updates = _build_update_properties(sender, existing)
        assert "firstname" not in updates
        assert "lastname" not in updates
        assert "company" not in updates

    def test_partial_update(self):
        sender = _make_sender(first_name="Mario", last_name="", company="Acme")
        existing = {"firstname": "Mario", "lastname": "", "company": ""}
        updates = _build_update_properties(sender, existing)
        assert "firstname" not in updates   # already set
        assert "company" in updates         # was blank


class TestSyncDeduplication:
    """Verify that _build_update_properties returns empty dict when nothing changed."""

    def test_no_updates_needed(self):
        sender = _make_sender()
        existing = {
            "firstname": "Mario",
            "lastname": "Rossi",
            "company": "Acme",
            "leadsource": "Gmail",
        }
        updates = _build_update_properties(sender, existing)
        assert updates == {}
