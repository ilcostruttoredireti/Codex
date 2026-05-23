"""Unit tests for HubSpot client helpers."""

import sys
import os

# allow importing from parent without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
from gmail_client import SenderInfo
from hubspot_client import SyncStatus, _build_properties


def _make_sender(**kwargs) -> SenderInfo:
    defaults = dict(
        email="mario@acme.com",
        first_name="Mario",
        last_name="Rossi",
        display_name="Mario Rossi",
        domain="acme.com",
        company="Acme",
        message_id="msg_001",
        thread_id="thread_001",
    )
    defaults.update(kwargs)
    return SenderInfo(**defaults)


def test_build_properties_new_contact():
    props = _build_properties(_make_sender(), existing=None)
    assert props["email"] == "mario@acme.com"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"
    assert props["hs_lead_source"] == "Gmail"


def test_build_properties_existing_filled():
    """Should not overwrite existing non-empty fields."""
    existing = MagicMock()
    existing.properties = {"firstname": "Existing", "lastname": "Name", "company": "OldCo"}
    props = _build_properties(_make_sender(), existing=existing)
    assert "firstname" not in props
    assert "lastname" not in props
    assert "company" not in props


def test_build_properties_existing_partial():
    """Should fill only the missing fields."""
    existing = MagicMock()
    existing.properties = {"firstname": "Mario", "lastname": "", "company": ""}
    props = _build_properties(_make_sender(), existing=existing)
    assert "firstname" not in props
    assert props.get("lastname") == "Rossi"
    assert props.get("company") == "Acme"


def test_build_properties_free_email_no_company():
    sender = _make_sender(email="user@gmail.com", domain="gmail.com", company="")
    props = _build_properties(sender, existing=None)
    assert "company" not in props
