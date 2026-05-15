from unittest.mock import MagicMock, patch
from gmail_hubspot_sync.sync_engine import SyncEngine, _should_skip


# ------------------------------------------------------------------
# _should_skip
# ------------------------------------------------------------------

def test_skip_noreply():
    assert _should_skip("noreply@example.com") is True
    assert _should_skip("no-reply@example.com") is True
    assert _should_skip("donotreply@example.com") is True


def test_no_skip_regular():
    assert _should_skip("mario@example.com") is False
    assert _should_skip("info@company.com") is False


# ------------------------------------------------------------------
# SyncEngine._process_message
# ------------------------------------------------------------------

def _make_engine(hubspot_result):
    gmail = MagicMock()
    hs = MagicMock()
    hs.sync_contact.return_value = hubspot_result
    return SyncEngine(gmail=gmail, hubspot=hs, poll_interval=60)


def test_process_creates_contact():
    engine = _make_engine({"status": "created", "email": "mario@acme.com", "id": "123"})
    result = engine._process_message({"from": "Mario Rossi <mario@acme.com>", "subject": "Ciao"})
    assert result["status"] == "created"
    assert result["email"] == "mario@acme.com"
    assert result["id"] == "123"


def test_process_skips_noreply():
    engine = _make_engine({})
    result = engine._process_message({"from": "noreply@service.com", "subject": "Alert"})
    assert result["status"] == "ignored"


def test_process_missing_from():
    engine = _make_engine({})
    result = engine._process_message({"from": "", "subject": "X"})
    assert result is None


def test_process_hubspot_error_returns_error_dict():
    gmail = MagicMock()
    hs = MagicMock()
    hs.sync_contact.side_effect = Exception("HubSpot down")
    engine = SyncEngine(gmail=gmail, hubspot=hs)
    result = engine._process_message({"from": "test@company.com", "subject": "Hi"})
    assert result["status"] == "error"
    assert "HubSpot down" in result["reason"]


def test_run_once_aggregates_results():
    gmail = MagicMock()
    gmail.get_new_messages.return_value = [
        {"from": "a@acme.com", "subject": "A"},
        {"from": "noreply@svc.com", "subject": "B"},
        {"from": "b@corp.io", "subject": "C"},
    ]
    hs = MagicMock()
    hs.sync_contact.return_value = {"status": "created", "email": "x@x.com", "id": "1"}
    engine = SyncEngine(gmail=gmail, hubspot=hs)
    results = engine.run_once()
    # 2 real + 1 ignored = 3 results
    assert len(results) == 3
    statuses = {r["status"] for r in results}
    assert "ignored" in statuses
