import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from unittest.mock import MagicMock, patch
import pytest

from sync import sync_message, SyncStatus


def _make_msg(from_header: str, subject: str = 'Test') -> dict:
    return {'id': 'msg1', 'from': from_header, 'subject': subject, 'date': ''}


def _mock_hubspot(existing=None):
    hs = MagicMock()
    hs.find_by_email.return_value = existing
    hs.create_contact.return_value = 'hs-new-001'
    hs.update_contact.return_value = True
    return hs


# ------------------------------------------------------------------
# Create path
# ------------------------------------------------------------------

def test_creates_new_contact():
    hs = _mock_hubspot(existing=None)
    result = sync_message(_make_msg('New User <new@company.com>'), hs, add_timeline=False)
    assert result.status == SyncStatus.CREATED
    assert result.email == 'new@company.com'
    assert result.contact_id == 'hs-new-001'
    hs.create_contact.assert_called_once()
    created_props = hs.create_contact.call_args[0][0]
    assert created_props['email'] == 'new@company.com'
    assert created_props['leadsource'] == 'Gmail'


def test_create_includes_name_and_company():
    hs = _mock_hubspot(existing=None)
    sync_message(_make_msg('Mario Rossi <mario@acme.com>'), hs, add_timeline=False)
    props = hs.create_contact.call_args[0][0]
    assert props['firstname'] == 'Mario'
    assert props['lastname'] == 'Rossi'
    assert props['company'] == 'Acme'


# ------------------------------------------------------------------
# Update path
# ------------------------------------------------------------------

def test_updates_existing_contact_missing_fields():
    existing = {'id': 'hs-123', 'properties': {'email': 'ex@corp.com', 'firstname': '', 'lastname': '', 'company': '', 'leadsource': ''}}
    hs = _mock_hubspot(existing=existing)
    result = sync_message(_make_msg('Jane Doe <ex@corp.com>'), hs, add_timeline=False)
    assert result.status == SyncStatus.UPDATED
    assert result.contact_id == 'hs-123'
    hs.update_contact.assert_called_once()
    patch_props = hs.update_contact.call_args[0][1]
    assert patch_props.get('firstname') == 'Jane'


def test_does_not_overwrite_existing_fields():
    existing = {
        'id': 'hs-123',
        'properties': {'firstname': 'ExistingName', 'lastname': 'ExistingLast', 'company': 'ExistingCo', 'leadsource': 'Web'},
    }
    hs = _mock_hubspot(existing=existing)
    sync_message(_make_msg('New Name <ex@corp.com>'), hs, add_timeline=False)
    patch_props = hs.update_contact.call_args[0][1] if hs.update_contact.called else {}
    assert 'firstname' not in patch_props
    assert 'lastname' not in patch_props
    assert 'company' not in patch_props


# ------------------------------------------------------------------
# Skip path
# ------------------------------------------------------------------

def test_skips_unparseable_header():
    hs = _mock_hubspot()
    result = sync_message({'id': 'x', 'from': '', 'subject': '', 'date': ''}, hs, add_timeline=False)
    assert result.status == SyncStatus.SKIPPED
    hs.create_contact.assert_not_called()
    hs.update_contact.assert_not_called()


# ------------------------------------------------------------------
# Error path
# ------------------------------------------------------------------

def test_create_failure_returns_error():
    hs = _mock_hubspot(existing=None)
    hs.create_contact.return_value = None
    result = sync_message(_make_msg('fail@company.com'), hs, add_timeline=False)
    assert result.status == SyncStatus.ERROR


def test_update_failure_returns_error():
    existing = {'id': 'hs-bad', 'properties': {}}
    hs = _mock_hubspot(existing=existing)
    hs.update_contact.return_value = False
    result = sync_message(_make_msg('Jane <jane@corp.com>'), hs, add_timeline=False)
    assert result.status == SyncStatus.ERROR


# ------------------------------------------------------------------
# Timeline
# ------------------------------------------------------------------

def test_timeline_note_called_on_create():
    hs = _mock_hubspot(existing=None)
    sync_message(_make_msg('new@corp.com', subject='Ciao'), hs, add_timeline=True)
    hs.add_email_note.assert_called_once_with('hs-new-001', 'Ciao')


def test_timeline_note_called_on_update():
    existing = {'id': 'hs-42', 'properties': {}}
    hs = _mock_hubspot(existing=existing)
    sync_message(_make_msg('ex@corp.com', subject='Re: Progetto'), hs, add_timeline=True)
    hs.add_email_note.assert_called_once_with('hs-42', 'Re: Progetto')


def test_no_timeline_when_disabled():
    hs = _mock_hubspot(existing=None)
    sync_message(_make_msg('new@corp.com'), hs, add_timeline=False)
    hs.add_email_note.assert_not_called()
