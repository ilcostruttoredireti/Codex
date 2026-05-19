import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from gmail_hubspot_sync.gmail_client import _parse_name, _is_skippable
import gmail_hubspot_sync.config as config


def test_parse_name_full():
    assert _parse_name("Mario Rossi") == ("Mario", "Rossi")


def test_parse_name_single():
    assert _parse_name("Mario") == ("Mario", "")


def test_parse_name_empty():
    assert _parse_name("") == ("", "")


def test_skip_noreply():
    assert _is_skippable("noreply@example.com") is True


def test_skip_known_domain():
    config.SKIP_DOMAINS.add("spam.test")
    assert _is_skippable("info@spam.test") is True
    config.SKIP_DOMAINS.discard("spam.test")


def test_valid_email_not_skipped():
    assert _is_skippable("mario.rossi@acme.com") is False
