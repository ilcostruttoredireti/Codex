from unittest.mock import MagicMock, patch
from gmail_hubspot_sync.sync_engine import SyncEngine, SyncStatus


def _make_engine(gmail_messages=(), existing_contact=None):
    gmail = MagicMock()
    gmail.get_inbox_messages.return_value = iter(gmail_messages)
    gmail.extract_headers.side_effect = lambda m: m.get("_headers", {})

    hubspot = MagicMock()
    hubspot.find_contact_by_email.return_value = existing_contact

    state = MagicMock()
    state.last_timestamp = None
    state.is_processed.return_value = False

    return SyncEngine(gmail=gmail, hubspot=hubspot, state=state)


def _msg(from_header, msg_id="msg1", date="Thu, 01 Jan 2026 10:00:00 +0000"):
    return {
        "id": msg_id,
        "_headers": {"from": from_header, "date": date},
        "payload": {"headers": [
            {"name": "From", "value": from_header},
            {"name": "Date", "value": date},
        ]},
    }


def test_new_contact_created():
    engine = _make_engine(
        gmail_messages=[_msg("Alice Smith <alice@company.com>")],
        existing_contact=None,
    )
    engine.hubspot.create_contact.return_value = {"id": "42"}
    results = engine.run_once()
    assert len(results) == 1
    assert results[0].status == SyncStatus.CREATED
    assert results[0].hubspot_id == "42"


def test_existing_contact_updated():
    existing = {"id": "99", "properties": {"firstname": "", "lastname": "", "company": ""}}
    engine = _make_engine(
        gmail_messages=[_msg("Bob Jones <bob@corp.io>")],
        existing_contact=existing,
    )
    engine.hubspot.update_contact.return_value = {"id": "99", "properties": {"firstname": "Bob"}}
    results = engine.run_once()
    assert results[0].status == SyncStatus.UPDATED
    assert results[0].hubspot_id == "99"


def test_noreply_skipped():
    engine = _make_engine(
        gmail_messages=[_msg("noreply@service.com")],
    )
    results = engine.run_once()
    assert results[0].status == SyncStatus.IGNORED
    engine.hubspot.create_contact.assert_not_called()


def test_already_processed_skipped():
    engine = _make_engine(
        gmail_messages=[_msg("test@example.com", msg_id="already")],
    )
    engine.state.is_processed.return_value = True
    results = engine.run_once()
    assert results == []


def test_no_from_header_skipped():
    engine = _make_engine(
        gmail_messages=[{"id": "x", "_headers": {}, "payload": {"headers": []}}],
    )
    results = engine.run_once()
    assert results == []


def test_empty_inbox_no_results():
    engine = _make_engine(gmail_messages=[])
    results = engine.run_once()
    assert results == []
