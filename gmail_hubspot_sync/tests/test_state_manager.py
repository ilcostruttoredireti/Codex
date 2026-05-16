import sys, os, json, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from state_manager import StateManager


def _tmp_state():
    f = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    f.close()
    os.unlink(f.name)   # start with no file
    return f.name


def test_initial_state_is_empty():
    sm = StateManager(_tmp_state())
    assert sm.history_id is None
    assert not sm.is_processed("abc")


def test_history_id_persists():
    path = _tmp_state()
    sm = StateManager(path)
    sm.history_id = "12345"
    sm2 = StateManager(path)
    assert sm2.history_id == "12345"
    os.unlink(path)


def test_mark_processed():
    path = _tmp_state()
    sm = StateManager(path)
    sm.mark_processed("msg-1", {"status": "created", "contact_id": "42"})
    assert sm.is_processed("msg-1")
    assert not sm.is_processed("msg-2")

    sm2 = StateManager(path)
    assert sm2.is_processed("msg-1")
    os.unlink(path)
