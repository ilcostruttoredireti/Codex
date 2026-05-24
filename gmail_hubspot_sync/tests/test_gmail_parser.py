"""
tests/test_gmail_parser.py — Unit test del parser email mittente.

Verifica l'estrazione di email, nome, cognome e dominio
dai vari formati dell'header From.

Importa solo da email_parser.py (nessuna dipendenza Google/HubSpot).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import unittest

from email_parser import SenderInfo, company_from_domain, parse_from_header


class TestFromHeaderParsing(unittest.TestCase):

    def test_full_name_and_email(self):
        info = parse_from_header("Mario Rossi <mario.rossi@acme.com>")
        self.assertIsNotNone(info)
        self.assertEqual(info.email, "mario.rossi@acme.com")
        self.assertEqual(info.first_name, "Mario")
        self.assertEqual(info.last_name, "Rossi")
        self.assertEqual(info.company_domain, "acme.com")

    def test_email_only(self):
        info = parse_from_header("mario@example.org")
        self.assertIsNotNone(info)
        self.assertEqual(info.email, "mario@example.org")
        self.assertEqual(info.first_name, "")
        self.assertEqual(info.last_name, "")

    def test_quoted_name(self):
        info = parse_from_header('"Giulia Bianchi" <giulia@startup.io>')
        self.assertIsNotNone(info)
        self.assertEqual(info.first_name, "Giulia")
        self.assertEqual(info.last_name, "Bianchi")

    def test_single_first_name(self):
        info = parse_from_header("Luca <luca@freelance.it>")
        self.assertIsNotNone(info)
        self.assertEqual(info.first_name, "Luca")
        self.assertEqual(info.last_name, "")

    def test_compound_last_name(self):
        info = parse_from_header("Anna Maria De Luca <anna@corp.com>")
        self.assertIsNotNone(info)
        self.assertEqual(info.first_name, "Anna")
        self.assertEqual(info.last_name, "Maria De Luca")

    def test_no_email_returns_none(self):
        info = parse_from_header("No Email Here")
        self.assertIsNone(info)

    def test_uppercase_email_lowercased(self):
        info = parse_from_header("Test User <TEST@DOMAIN.COM>")
        self.assertIsNotNone(info)
        self.assertEqual(info.email, "test@domain.com")

    def test_subdomain(self):
        info = parse_from_header("bot@mail.google.com")
        self.assertIsNotNone(info)
        self.assertEqual(info.company_domain, "mail.google.com")

    def test_ignore_domains_filters(self):
        info = parse_from_header(
            "newsletter@mailchimp.com",
            ignore_domains=frozenset(["mailchimp.com"]),
        )
        self.assertIsNone(info)

    def test_ignore_domains_allows_others(self):
        info = parse_from_header(
            "mario@legit.com",
            ignore_domains=frozenset(["mailchimp.com"]),
        )
        self.assertIsNotNone(info)
        self.assertEqual(info.email, "mario@legit.com")

    def test_reply_to_style_header(self):
        # Formato reply-to senza nome
        info = parse_from_header("no-reply@service.com")
        self.assertIsNotNone(info)
        self.assertEqual(info.email, "no-reply@service.com")

    def test_message_id_propagated(self):
        info = parse_from_header("a@b.com", message_id="abc123")
        self.assertIsNotNone(info)
        self.assertEqual(info.message_id, "abc123")


class TestCompanyFromDomain(unittest.TestCase):

    def test_simple_domain(self):
        self.assertEqual(company_from_domain("acme.com"), "Acme")

    def test_subdomain(self):
        self.assertEqual(company_from_domain("mail.google.com"), "Google")

    def test_single_part(self):
        self.assertEqual(company_from_domain("localhost"), "Localhost")

    def test_capitalisation(self):
        self.assertEqual(company_from_domain("STARTUP.IO"), "Startup")


if __name__ == "__main__":
    unittest.main()
