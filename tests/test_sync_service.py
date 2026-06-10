from unittest.mock import MagicMock, patch
from gmail_hubspot_sync.gmail_client import EmailMessage
from gmail_hubspot_sync.hubspot_client import SyncOutcome, SyncResult
from gmail_hubspot_sync.sync_service import SyncService


def _make_email_message(from_header="Mario Rossi <mario@acme.com>", subject="Ciao"):
    return EmailMessage(
        message_id="msg-001",
        from_header=from_header,
        subject=subject,
        date="Mon, 10 Jun 2026 10:00:00 +0200",
        thread_id="thread-001",
    )


def _make_service(enable_note=False):
    gmail = MagicMock()
    hubspot = MagicMock()
    gmail.get_unprocessed_inbox_messages.return_value = [_make_email_message()]
    service = SyncService(
        gmail=gmail,
        hubspot=hubspot,
        processed_label_name="HubSpot-Processed",
        enable_activity_note=enable_note,
    )
    service._processed_label_id = "label-123"
    return service, gmail, hubspot


def test_run_once_creates_new_contact():
    service, gmail, hubspot = _make_service()
    hubspot.find_contact_by_email.return_value = None
    hubspot.create_contact.return_value = SyncOutcome(
        result=SyncResult.CREATED, contact_email="mario@acme.com", contact_id="hs-1"
    )

    outcomes = service.run_once()

    assert len(outcomes) == 1
    assert outcomes[0].result == SyncResult.CREATED
    hubspot.create_contact.assert_called_once()
    gmail.mark_as_processed.assert_called_once_with("msg-001", "label-123")


def test_run_once_updates_existing_contact():
    service, gmail, hubspot = _make_service()
    existing = MagicMock()
    existing.id = "hs-existing"
    hubspot.find_contact_by_email.return_value = existing
    hubspot.update_contact.return_value = SyncOutcome(
        result=SyncResult.UPDATED,
        contact_email="mario@acme.com",
        contact_id="hs-existing",
        detail="aggiornati: company",
    )

    outcomes = service.run_once()

    assert outcomes[0].result == SyncResult.UPDATED
    hubspot.update_contact.assert_called_once()


def test_run_once_skips_unparsable_from():
    service, gmail, hubspot = _make_service()
    gmail.get_unprocessed_inbox_messages.return_value = [
        _make_email_message(from_header="not-valid-header")
    ]

    outcomes = service.run_once()

    assert outcomes[0].result == SyncResult.SKIPPED
    hubspot.create_contact.assert_not_called()
    # Marca comunque come processato
    gmail.mark_as_processed.assert_called_once()


def test_run_once_empty_inbox():
    service, gmail, hubspot = _make_service()
    gmail.get_unprocessed_inbox_messages.return_value = []

    outcomes = service.run_once()

    assert outcomes == []
    hubspot.create_contact.assert_not_called()


def test_run_once_creates_activity_note_when_enabled():
    service, gmail, hubspot = _make_service(enable_note=True)
    hubspot.find_contact_by_email.return_value = None
    hubspot.create_contact.return_value = SyncOutcome(
        result=SyncResult.CREATED, contact_email="mario@acme.com", contact_id="hs-1"
    )

    service.run_once()

    hubspot.create_activity_note.assert_called_once_with(
        contact_id="hs-1",
        subject="Ciao",
        email_date="Mon, 10 Jun 2026 10:00:00 +0200",
    )


def test_run_once_no_activity_note_when_disabled():
    service, gmail, hubspot = _make_service(enable_note=False)
    hubspot.find_contact_by_email.return_value = None
    hubspot.create_contact.return_value = SyncOutcome(
        result=SyncResult.CREATED, contact_email="mario@acme.com", contact_id="hs-1"
    )

    service.run_once()

    hubspot.create_activity_note.assert_not_called()
