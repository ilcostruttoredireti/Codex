"""Unit tests for the sync orchestration layer."""

from unittest.mock import MagicMock

from gmail_hubspot_sync.sync import GmailHubSpotSync


def _make_sender(email="user@acme.io", domain="acme.io"):
    return {
        "message_id": "msg-001",
        "email": email,
        "full_name": "Test User",
        "first_name": "Test",
        "last_name": "User",
        "domain": domain,
        "subject": "Hello",
        "date": "Mon, 1 Jan 2024 10:00:00 +0000",
    }


def _make_syncer(gmail_senders, hubspot_return, ignored_domains=None, mark_read=False):
    gmail = MagicMock()
    gmail.fetch_unread.return_value = gmail_senders

    hubspot = MagicMock()
    hubspot.upsert_contact.return_value = hubspot_return

    return GmailHubSpotSync(
        gmail=gmail,
        hubspot=hubspot,
        ignored_domains=ignored_domains or set(),
        mark_read=mark_read,
    ), gmail, hubspot


def test_creates_new_contact():
    sender = _make_sender()
    syncer, gmail, hubspot = _make_syncer([sender], ("created", "hs-123"))

    results = syncer.run_once()

    assert len(results) == 1
    assert results[0].status == "created"
    assert results[0].contact_id == "hs-123"
    assert results[0].email == "user@acme.io"
    hubspot.upsert_contact.assert_called_once_with(sender)


def test_updates_existing_contact():
    sender = _make_sender()
    syncer, _, hubspot = _make_syncer([sender], ("updated", "hs-456"))

    results = syncer.run_once()

    assert results[0].status == "updated"
    assert results[0].contact_id == "hs-456"


def test_ignored_when_no_changes():
    sender = _make_sender()
    syncer, _, _ = _make_syncer([sender], ("ignored", "hs-789"))

    results = syncer.run_once()

    assert results[0].status == "ignored"


def test_skips_ignored_domain():
    sender = _make_sender(email="bot@noreply.com", domain="noreply.com")
    syncer, _, hubspot = _make_syncer(
        [sender], ("created", "x"), ignored_domains={"noreply.com"}
    )

    results = syncer.run_once()

    assert results[0].status == "skipped"
    hubspot.upsert_contact.assert_not_called()


def test_marks_message_read_after_sync():
    sender = _make_sender()
    syncer, gmail, _ = _make_syncer([sender], ("created", "hs-123"), mark_read=True)

    syncer.run_once()

    gmail.mark_as_read.assert_called_once_with("msg-001")


def test_error_from_hubspot_returns_error_status():
    sender = _make_sender()
    syncer, gmail, hubspot = _make_syncer([sender], None, mark_read=False)
    hubspot.upsert_contact.side_effect = RuntimeError("API down")

    results = syncer.run_once()

    assert results[0].status == "error"
    gmail.mark_as_read.assert_not_called()


def test_no_messages_returns_empty():
    syncer, _, _ = _make_syncer([], ("created", "x"))
    assert syncer.run_once() == []
