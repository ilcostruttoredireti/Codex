"""Unit tests for the email-skip filter."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from filters import should_skip as _should_skip


class TestShouldSkip:
    def test_valid_sender(self):
        assert _should_skip("alice@acme.com") is False

    def test_noreply(self):
        assert _should_skip("noreply@company.com") is True

    def test_no_reply_hyphen(self):
        assert _should_skip("no-reply@service.io") is True

    def test_mailer_daemon(self):
        assert _should_skip("mailer-daemon@mail.com") is True

    def test_newsletter(self):
        assert _should_skip("newsletter@example.com") is True

    def test_empty_string(self):
        assert _should_skip("") is True

    def test_no_at_sign(self):
        assert _should_skip("notanemail") is True

    def test_skip_domain(self):
        assert _should_skip("anything@noreply.com") is True

    def test_mixed_case(self):
        assert _should_skip("NoReply@COMPANY.COM") is True

    def test_notifications(self):
        assert _should_skip("notifications@github.com") is True
