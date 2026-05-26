"""Test per le funzioni di parsing (parsing_utils.py)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from parsing_utils import (
    parse_from_header,
    parse_name,
    extract_domain,
    domain_to_company,
)


class TestParseFromHeader:
    def test_full_format_with_quotes(self):
        display, addr = parse_from_header('"Mario Rossi" <mario@example.com>')
        assert display == "Mario Rossi"
        assert addr == "mario@example.com"

    def test_no_quotes(self):
        display, addr = parse_from_header("Mario Rossi <mario@example.com>")
        assert display == "Mario Rossi"
        assert addr == "mario@example.com"

    def test_only_email(self):
        display, addr = parse_from_header("mario@example.com")
        assert display == ""
        assert addr == "mario@example.com"

    def test_email_uppercase_normalized(self):
        _, addr = parse_from_header("Mario <MARIO@Example.COM>")
        assert addr == "mario@example.com"

    def test_empty_string(self):
        display, addr = parse_from_header("")
        assert display == ""
        assert addr == ""

    def test_single_quotes_stripped(self):
        display, addr = parse_from_header("'Mario' <mario@test.com>")
        assert display == "Mario"
        assert addr == "mario@test.com"


class TestParseName:
    def test_full_name(self):
        first, last = parse_name("Mario Rossi")
        assert first == "Mario"
        assert last == "Rossi"

    def test_single_name(self):
        first, last = parse_name("Mario")
        assert first == "Mario"
        assert last == ""

    def test_compound_last_name(self):
        first, last = parse_name("Anna Maria De Luca")
        assert first == "Anna"
        assert last == "Maria De Luca"

    def test_empty(self):
        first, last = parse_name("")
        assert first == ""
        assert last == ""

    def test_strips_double_quotes(self):
        first, last = parse_name('"Mario Rossi"')
        assert first == "Mario"
        assert last == "Rossi"

    def test_strips_single_quotes(self):
        first, last = parse_name("'Mario Rossi'")
        assert first == "Mario"
        assert last == "Rossi"


class TestExtractDomain:
    def test_normal_email(self):
        assert extract_domain("user@example.com") == "example.com"

    def test_uppercase_normalized(self):
        assert extract_domain("USER@EXAMPLE.COM") == "example.com"

    def test_no_at_symbol(self):
        assert extract_domain("notanemail") == ""

    def test_subdomain(self):
        assert extract_domain("user@mail.example.com") == "mail.example.com"

    def test_empty(self):
        assert extract_domain("") == ""


class TestDomainToCompany:
    def test_simple(self):
        assert domain_to_company("acme.com") == "Acme"

    def test_hyphen(self):
        assert domain_to_company("acme-corp.com") == "Acme Corp"

    def test_underscore(self):
        assert domain_to_company("acme_corp.it") == "Acme Corp"

    def test_empty(self):
        assert domain_to_company("") == ""

    def test_subdomain_included(self):
        # mail.example.com → rimuove .com → "mail.example" → capitalizza prima parola
        # Il risultato è "Mail.example" (comportamento atteso — nessun split su '.')
        result = domain_to_company("mail.example.com")
        assert result == "Mail.example"
