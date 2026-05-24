"""
Integration-style unit tests for the GmailHubSpotSyncer orchestrator.
All external I/O is mocked.
"""
import sys
import os
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from models import EmailMessage, SenderInfo, SyncResult, SyncStatus
from sync import GmailHubSpotSyncer


def _make_email(
    msg_id: str = "msg1",
    email: str = "sender@acme.com",
    first: str = "John",
    last: str = "Doe",
    company: str = "Acme",
    subject: str = "Hello",
) -> EmailMessage:
    return EmailMessage(
        message_id=msg_id,
        thread_id="t1",
        subject=subject,
        sender=SenderInfo(
            email=email,
            first_name=first,
            last_name=last,
            full_name=f"{first} {last}",
            company=company,
        ),
        received_at="Mon, 01 Jan 2024 10:00:00 +0000",
        snippet="snippet…",
    )


class TestRunOnce:
    def _setup_syncer(self, messages, find_result=None, create_result=None, update_result=None):
        gmail = MagicMock()
        gmail.fetch_new_messages.return_value = iter(messages)
        gmail.mark_as_processed = MagicMock()

        hubspot = MagicMock()
        hubspot.find_contact_by_email.return_value = find_result
        if create_result:
            hubspot.create_contact.return_value = create_result
        if update_result:
            hubspot.update_contact.return_value = update_result
        hubspot.create_email_activity = MagicMock()

        syncer = GmailHubSpotSyncer(gmail=gmail, hubspot=hubspot)
        return syncer, gmail, hubspot

    def test_creates_new_contact_when_not_found(self):
        msg = _make_email()
        created = SyncResult(SyncStatus.CREATED, "sender@acme.com", hubspot_id="hs1")

        syncer, gmail, hs = self._setup_syncer([msg], find_result=None, create_result=created)
        results = syncer.run_once(create_activities=False)

        assert len(results) == 1
        assert results[0].status == SyncStatus.CREATED
        assert results[0].hubspot_id == "hs1"
        hs.create_contact.assert_called_once()
        gmail.mark_as_processed.assert_called_once_with("msg1")

    def test_updates_existing_contact(self):
        msg = _make_email()
        existing = {"id": "hs99", "properties": {"firstname": "", "company": "Acme"}}
        updated = SyncResult(SyncStatus.UPDATED, "sender@acme.com", hubspot_id="hs99")

        syncer, gmail, hs = self._setup_syncer([msg], find_result=existing, update_result=updated)
        results = syncer.run_once(create_activities=False)

        assert results[0].status == SyncStatus.UPDATED
        hs.update_contact.assert_called_once()
        hs.create_contact.assert_not_called()

    def test_deduplicates_same_sender_in_one_pass(self):
        """Two messages from same address → only one HubSpot call."""
        msg1 = _make_email(msg_id="m1")
        msg2 = _make_email(msg_id="m2")  # same email address
        created = SyncResult(SyncStatus.CREATED, "sender@acme.com", hubspot_id="hs1")

        syncer, gmail, hs = self._setup_syncer([msg1, msg2], find_result=None, create_result=created)
        results = syncer.run_once(create_activities=False)

        assert len(results) == 2
        assert results[0].status == SyncStatus.CREATED
        assert results[1].status == SyncStatus.IGNORED
        hs.create_contact.assert_called_once()

    def test_both_messages_labelled_even_when_ignored(self):
        msg1 = _make_email(msg_id="m1")
        msg2 = _make_email(msg_id="m2")
        created = SyncResult(SyncStatus.CREATED, "sender@acme.com", hubspot_id="hs1")

        syncer, gmail, hs = self._setup_syncer([msg1, msg2], find_result=None, create_result=created)
        syncer.run_once(create_activities=False)

        gmail.mark_as_processed.assert_any_call("m1")
        gmail.mark_as_processed.assert_any_call("m2")

    def test_no_messages_returns_empty_list(self):
        syncer, _, _ = self._setup_syncer([])
        results = syncer.run_once()
        assert results == []

    def test_activity_created_for_new_contact(self):
        msg = _make_email()
        created = SyncResult(SyncStatus.CREATED, "sender@acme.com", hubspot_id="hs1")

        syncer, _, hs = self._setup_syncer([msg], find_result=None, create_result=created)
        syncer.run_once(create_activities=True)

        hs.create_email_activity.assert_called_once()
