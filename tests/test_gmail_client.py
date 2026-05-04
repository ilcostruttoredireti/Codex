"""Unit tests for gmail_client helpers."""

from gmail_hubspot_sync.gmail_client import _split_name


def test_split_name_two_parts():
    assert _split_name("Mario Rossi") == ("Mario", "Rossi")


def test_split_name_single():
    assert _split_name("Mario") == ("Mario", "")


def test_split_name_empty():
    assert _split_name("") == ("", "")


def test_split_name_three_parts():
    # Only splits on the first space — last name becomes "Rossi Jr"
    first, last = _split_name("Mario Rossi Jr")
    assert first == "Mario"
    assert last == "Rossi Jr"
