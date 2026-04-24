"""Unit tests for gmail_hubspot_sync — no MCP or network required."""

import unittest
from unittest.mock import MagicMock, call
from gmail_hubspot_sync import (
    SenderInfo,
    SyncResult,
    process_email,
    _company_from_email,
    _split_name,
    _is_valid_email,
)


class TestSenderInfo(unittest.TestCase):

    def test_full_name_corporate(self):
        s = SenderInfo.from_header("Mario Rossi <mario.rossi@acme.com>")
        self.assertEqual(s.email, "mario.rossi@acme.com")
        self.assertEqual(s.first_name, "Mario")
        self.assertEqual(s.last_name, "Rossi")
        self.assertEqual(s.company, "Acme")

    def test_generic_domain_no_company(self):
        s = SenderInfo.from_header("Luca Bianchi <luca@gmail.com>")
        self.assertEqual(s.company, "")

    def test_no_display_name(self):
        s = SenderInfo.from_header("info@startup.io")
        self.assertEqual(s.email, "info@startup.io")
        self.assertEqual(s.first_name, "")
        self.assertEqual(s.company, "Startup")

    def test_email_lowercased(self):
        s = SenderInfo.from_header("TEST@EXAMPLE.COM")
        self.assertEqual(s.email, "test@example.com")


class TestHelpers(unittest.TestCase):

    def test_split_name(self):
        self.assertEqual(_split_name("Mario Rossi"), ("Mario", "Rossi"))
        self.assertEqual(_split_name("Mario"), ("Mario", ""))
        self.assertEqual(_split_name(""), ("", ""))

    def test_company_from_email(self):
        self.assertEqual(_company_from_email("user@openai.com"), "Openai")
        self.assertEqual(_company_from_email("user@gmail.com"), "")
        self.assertEqual(_company_from_email("badformat"), "")

    def test_is_valid_email(self):
        self.assertTrue(_is_valid_email("a@b.com"))
        self.assertFalse(_is_valid_email("notanemail"))
        self.assertFalse(_is_valid_email("@domain.com"))


class TestProcessEmail(unittest.TestCase):

    def _make_tools(self, existing_contact=None, create_id="999"):
        tools = MagicMock()
        tools.search_crm_objects.return_value = {
            "results": [existing_contact] if existing_contact else []
        }
        tools.manage_crm_objects.return_value = {
            "results": [{"id": create_id}]
        }
        return tools

    def test_creates_new_contact(self):
        tools = self._make_tools()
        result = process_email("Mario Rossi <mario@acme.com>", tools)
        self.assertEqual(result.status, "created")
        self.assertEqual(result.email, "mario@acme.com")
        self.assertEqual(result.hubspot_id, "999")
        tools.manage_crm_objects.assert_called_once()
        create_args = tools.manage_crm_objects.call_args[1]["createRequest"]
        props = create_args["objects"][0]["properties"]
        self.assertEqual(props["email"], "mario@acme.com")
        self.assertEqual(props["hs_lead_source"], "Gmail")

    def test_updates_existing_contact_with_missing_fields(self):
        existing = {
            "id": "42",
            "properties": {"email": "mario@acme.com", "firstname": "", "lastname": "", "company": ""},
        }
        tools = self._make_tools(existing_contact=existing)
        result = process_email("Mario Rossi <mario@acme.com>", tools)
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.hubspot_id, "42")
        update_args = tools.manage_crm_objects.call_args[1]["updateRequest"]
        props = update_args["objects"][0]["properties"]
        self.assertEqual(props["firstname"], "Mario")

    def test_ignores_when_no_new_data(self):
        existing = {
            "id": "42",
            "properties": {
                "email": "mario@acme.com",
                "firstname": "Mario",
                "lastname": "Rossi",
                "company": "Acme",
                "hs_lead_source": "Gmail",
            },
        }
        tools = self._make_tools(existing_contact=existing)
        result = process_email("Mario Rossi <mario@acme.com>", tools)
        self.assertEqual(result.status, "ignored")
        tools.manage_crm_objects.assert_not_called()

    def test_ignores_noreply(self):
        tools = self._make_tools()
        result = process_email("noreply@service.com", tools)
        self.assertEqual(result.status, "ignored")
        tools.search_crm_objects.assert_not_called()

    def test_ignores_invalid_email(self):
        tools = self._make_tools()
        result = process_email("not-an-email", tools)
        self.assertEqual(result.status, "ignored")

    def test_deduplication_key_is_email(self):
        """Two calls with the same email must search with the same key."""
        tools = self._make_tools()
        process_email("Alice Smith <alice@corp.com>", tools)
        process_email("alice@corp.com", tools)
        for c in tools.search_crm_objects.call_args_list:
            email_filter = c[1]["filterGroups"][0]["filters"][0]
            self.assertEqual(email_filter["value"], "alice@corp.com")


if __name__ == "__main__":
    unittest.main()
