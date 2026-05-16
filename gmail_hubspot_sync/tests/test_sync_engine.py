import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from unittest.mock import MagicMock, patch
from models import SenderInfo
from hubspot_client import ContactResult
from sync_engine import SyncEngine


def _make_sender(email: str, name: str = "Mario Rossi", domain: str = "") -> SenderInfo:
    s = MagicMock(spec=SenderInfo)
    s.email = email
    s.name = name
    s.domain = domain or email.split("@")[1]
    s.message_id = "msg-001"
    s.subject = "Test subject"
    return s


def _make_engine(hs_mock, ignored=None):
    return SyncEngine(
        hubspot=hs_mock,
        source="Gmail",
        tag="Inbound Gmail",
        ignored_domains=ignored or [],
        add_timeline=False,
    )


# ── ignored domain ────────────────────────────────────────────────────────────

def test_ignored_domain():
    hs = MagicMock()
    engine = _make_engine(hs, ignored=["skip.com"])
    sender = _make_sender("user@skip.com", domain="skip.com")
    result = engine.process(sender)
    assert result.status == "ignored"
    hs.find_contact_by_email.assert_not_called()


# ── new contact ───────────────────────────────────────────────────────────────

def test_creates_new_contact():
    hs = MagicMock()
    hs.find_contact_by_email.return_value = None
    hs.create_contact.return_value = "42"
    engine = _make_engine(hs)
    sender = _make_sender("mario@acme.com")
    result = engine.process(sender)
    assert result.status == "created"
    assert result.contact_id == "42"
    hs.create_contact.assert_called_once()


def test_create_failure_returns_error():
    hs = MagicMock()
    hs.find_contact_by_email.return_value = None
    hs.create_contact.return_value = None   # simulate API failure
    engine = _make_engine(hs)
    sender = _make_sender("mario@acme.com")
    result = engine.process(sender)
    assert result.status == "error"


# ── existing contact ──────────────────────────────────────────────────────────

def _existing_contact(contact_id="99", props=None):
    c = MagicMock()
    c.id = contact_id
    c.properties = props or {"firstname": "Mario", "lastname": "Rossi", "company": "Acme"}
    return c


def test_updates_missing_fields():
    hs = MagicMock()
    hs.find_contact_by_email.return_value = _existing_contact(props={"firstname": "", "lastname": "", "company": ""})
    hs.update_contact.return_value = True
    engine = _make_engine(hs)
    sender = _make_sender("mario@acme.com")
    result = engine.process(sender)
    assert result.status == "updated"
    assert result.contact_id == "99"


def test_skips_when_all_fields_present():
    hs = MagicMock()
    hs.find_contact_by_email.return_value = _existing_contact(
        props={"firstname": "Mario", "lastname": "Rossi", "company": "Acme", "leadsource": "Gmail"}
    )
    engine = _make_engine(hs)
    sender = _make_sender("mario@acme.com")
    result = engine.process(sender)
    assert result.status == "skipped"
    hs.update_contact.assert_not_called()


# ── timeline ──────────────────────────────────────────────────────────────────

def test_timeline_called_on_create():
    hs = MagicMock()
    hs.find_contact_by_email.return_value = None
    hs.create_contact.return_value = "77"
    engine = SyncEngine(hs, "Gmail", "Inbound Gmail", [], add_timeline=True)
    sender = _make_sender("new@acme.com")
    engine.process(sender)
    hs.add_timeline_event.assert_called_once_with("77", "new@acme.com", sender.subject)
