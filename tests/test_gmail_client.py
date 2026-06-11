import pytest
from src.gmail_client import NO_REPLY_PATTERN


def test_no_reply_pattern_matches():
    no_reply_locals = [
        "noreply", "no-reply", "no_reply", "donotreply",
        "do-not-reply", "do_not_reply", "postmaster", "mailer-daemon", "bounce",
    ]
    for local in no_reply_locals:
        assert NO_REPLY_PATTERN.match(local), f"Should match: {local}"


def test_no_reply_pattern_does_not_match_normal():
    normal_locals = ["mario", "info", "support", "hello", "admin", "sales"]
    for local in normal_locals:
        assert not NO_REPLY_PATTERN.match(local), f"Should not match: {local}"
