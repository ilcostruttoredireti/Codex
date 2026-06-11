"""
Unit tests for pure helper functions in utils.py.
No external API calls or heavy SDK imports.
"""
from __future__ import annotations

import pytest

from utils import company_from_domain, extract_sender, parse_name, should_ignore


# ── extract_sender ────────────────────────────────────────────────────────────

class TestExtractSender:
    def test_name_and_angle_brackets(self):
        name, email = extract_sender("John Doe <john@example.com>")
        assert name == "John Doe"
        assert email == "john@example.com"

    def test_quoted_name(self):
        name, email = extract_sender('"Jane Smith" <jane@company.org>')
        assert name == "Jane Smith"
        assert email == "jane@company.org"

    def test_bare_address(self):
        name, email = extract_sender("info@startup.io")
        assert name == ""
        assert email == "info@startup.io"

    def test_lowercase_normalisation(self):
        _, email = extract_sender("UPPER@DOMAIN.COM")
        assert email == "upper@domain.com"


# ── parse_name ────────────────────────────────────────────────────────────────

class TestParseName:
    def test_first_last(self):
        assert parse_name("Alice Brown") == ("Alice", "Brown")

    def test_single_name(self):
        assert parse_name("Madonna") == ("Madonna", "")

    def test_multi_word_last(self):
        first, last = parse_name("Maria De Luca")
        assert first == "Maria"
        assert last == "De Luca"

    def test_empty(self):
        assert parse_name("") == ("", "")


# ── company_from_domain ───────────────────────────────────────────────────────

class TestCompanyFromDomain:
    def test_simple(self):
        assert company_from_domain("acme.com") == "Acme"

    def test_subdomain(self):
        assert company_from_domain("mail.bigcorp.io") == "Bigcorp"

    def test_single_label(self):
        assert company_from_domain("localhost") == "Localhost"


# ── should_ignore ─────────────────────────────────────────────────────────────

class TestShouldIgnore:
    def test_ignored_prefix_noreply(self):
        assert should_ignore("noreply@example.com") is True

    def test_ignored_prefix_with_dash(self):
        assert should_ignore("no-reply@company.com") is True

    def test_normal_address_not_ignored(self):
        assert should_ignore("alice@company.com") is False

    def test_ignored_domain(self):
        assert should_ignore("user@noreply.com") is True
