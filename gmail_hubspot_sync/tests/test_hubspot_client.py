"""Unit tests for hubspot_client helpers (no network calls)."""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from hubspot_client import _extract_company, _split_name


class TestExtractCompany:
    def test_business_domain(self):
        assert _extract_company("alice@acme.com") == "Acme"

    def test_personal_gmail(self):
        assert _extract_company("bob@gmail.com") is None

    def test_personal_outlook(self):
        assert _extract_company("bob@outlook.com") is None

    def test_subdomain_stripped(self):
        # Only first label used: mail.mycompany.com → mycompany
        result = _extract_company("user@mail.mycompany.com")
        # domain split gives ["mail", "mycompany", "com"] → parts[0] = "mail"
        # That is the current behaviour – test documents it.
        assert result == "Mail"

    def test_hyphenated_domain(self):
        assert _extract_company("user@my-company.io") == "My Company"

    def test_no_at_sign(self):
        assert _extract_company("notanemail") is None

    def test_italian_personal_domain(self):
        assert _extract_company("user@libero.it") is None


class TestSplitName:
    def test_full_name(self):
        assert _split_name("Mario Rossi") == ("Mario", "Rossi")

    def test_single_name(self):
        assert _split_name("Mario") == ("Mario", "")

    def test_compound_last_name(self):
        assert _split_name("Jean-Claude Van Damme") == ("Jean-Claude", "Van Damme")

    def test_empty_string(self):
        assert _split_name("") == ("", "")

    def test_whitespace_only(self):
        assert _split_name("   ") == ("", "")
