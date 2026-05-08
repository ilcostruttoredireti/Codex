"""Unit tests for pure-Python parsing helpers (no network / SDK required)."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils import parse_sender, extract_name_parts, company_from_domain, is_no_reply


class TestParseSender(unittest.TestCase):

    def test_with_name(self):
        r = parse_sender("Mario Rossi <mario.rossi@acme.com>")
        self.assertEqual(r["email"], "mario.rossi@acme.com")
        self.assertEqual(r["name"], "Mario Rossi")
        self.assertEqual(r["domain"], "acme.com")

    def test_without_name(self):
        r = parse_sender("mario.rossi@acme.com")
        self.assertEqual(r["email"], "mario.rossi@acme.com")
        self.assertEqual(r["name"], "")
        self.assertEqual(r["domain"], "acme.com")

    def test_quoted_name(self):
        r = parse_sender('"Acme Support" <support@acme.io>')
        self.assertEqual(r["email"], "support@acme.io")
        self.assertEqual(r["name"], "Acme Support")


class TestExtractNameParts(unittest.TestCase):

    def test_single(self):
        self.assertEqual(extract_name_parts("Mario"), ("Mario", ""))

    def test_two_parts(self):
        self.assertEqual(extract_name_parts("Mario Rossi"), ("Mario", "Rossi"))

    def test_three_parts(self):
        self.assertEqual(extract_name_parts("Mario De Rossi"), ("Mario", "De Rossi"))


class TestCompanyFromDomain(unittest.TestCase):

    def test_business_domain(self):
        self.assertEqual(company_from_domain("acme.com"), "Acme")

    def test_public_domain(self):
        self.assertEqual(company_from_domain("gmail.com"), "")

    def test_empty(self):
        self.assertEqual(company_from_domain(""), "")

    def test_subdomain_first_segment(self):
        self.assertEqual(company_from_domain("mail.acme.it"), "Mail")


class TestIsNoReply(unittest.TestCase):

    def test_noreply(self):
        self.assertTrue(is_no_reply("noreply@example.com"))

    def test_no_dash_reply(self):
        self.assertTrue(is_no_reply("no-reply@example.com"))

    def test_donotreply(self):
        self.assertTrue(is_no_reply("donotreply@example.com"))

    def test_normal_address(self):
        self.assertFalse(is_no_reply("mario@acme.com"))


if __name__ == "__main__":
    unittest.main()
