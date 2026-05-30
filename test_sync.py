"""Test unitari per gmail_hubspot_sync.py"""

import json
import unittest
from unittest.mock import MagicMock, patch
from pathlib import Path
import tempfile
import os

# Patch le variabili d'ambiente prima dell'import
os.environ.setdefault("HUBSPOT_TOKEN", "test-token")


class TestParseSender(unittest.TestCase):

    def _parse(self, from_header, **kwargs):
        from gmail_hubspot_sync import parse_sender
        return parse_sender(from_header, **kwargs)

    def test_full_name_and_email(self):
        result = self._parse("Mario Rossi <mario.rossi@acme.com>")
        self.assertIsNotNone(result)
        self.assertEqual(result.email, "mario.rossi@acme.com")
        self.assertEqual(result.first_name, "Mario")
        self.assertEqual(result.last_name, "Rossi")
        self.assertEqual(result.company, "Acme")
        self.assertEqual(result.domain, "acme.com")

    def test_email_only(self):
        result = self._parse("info@startup.io")
        self.assertIsNotNone(result)
        self.assertEqual(result.email, "info@startup.io")
        self.assertEqual(result.first_name, "")
        self.assertEqual(result.company, "Startup")

    def test_personal_domain_no_company(self):
        result = self._parse("Luca Bianchi <luca@gmail.com>")
        self.assertIsNotNone(result)
        self.assertEqual(result.company, "")

    def test_noreply_filtered(self):
        result = self._parse("noreply@service.com")
        self.assertIsNone(result)

    def test_no_reply_hyphen_filtered(self):
        result = self._parse("no-reply@newsletter.it")
        self.assertIsNone(result)

    def test_invalid_email_filtered(self):
        result = self._parse("Non un'email valida")
        self.assertIsNone(result)

    def test_ignored_domain(self):
        import gmail_hubspot_sync as m
        original = m.IGNORED_DOMAINS.copy()
        m.IGNORED_DOMAINS.add("spam.com")
        result = self._parse("user@spam.com")
        self.assertIsNone(result)
        m.IGNORED_DOMAINS = original

    def test_single_name(self):
        result = self._parse("Cristian <cristian@company.it>")
        self.assertIsNotNone(result)
        self.assertEqual(result.first_name, "Cristian")
        self.assertEqual(result.last_name, "")

    def test_email_uppercase_normalized(self):
        result = self._parse("TEST@ACME.COM")
        self.assertIsNotNone(result)
        self.assertEqual(result.email, "test@acme.com")

    def test_subject_and_date_parsed(self):
        result = self._parse(
            "Mario <mario@acme.com>",
            subject="Proposta commerciale",
            date_header="Mon, 26 May 2025 10:00:00 +0200",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result.subject, "Proposta commerciale")
        self.assertIn("2025", result.received_at)


class TestSyncState(unittest.TestCase):

    def test_save_and_load(self):
        from gmail_hubspot_sync import SyncState
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        try:
            state = SyncState(last_sync_ts=12345, processed_message_ids=["id1", "id2"])
            state.save(path)
            loaded = SyncState.load(path)
            self.assertEqual(loaded.last_sync_ts, 12345)
            self.assertIn("id1", loaded.processed_message_ids)
        finally:
            path.unlink(missing_ok=True)

    def test_load_missing_file(self):
        from gmail_hubspot_sync import SyncState
        state = SyncState.load(Path("/tmp/does_not_exist_xyz.json"))
        self.assertEqual(state.last_sync_ts, 0)
        self.assertEqual(state.processed_message_ids, [])

    def test_processed_ids_capped_at_5000(self):
        from gmail_hubspot_sync import SyncState
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = Path(f.name)
        try:
            state = SyncState(processed_message_ids=[str(i) for i in range(6000)])
            state.save(path)
            loaded = SyncState.load(path)
            self.assertEqual(len(loaded.processed_message_ids), 5000)
        finally:
            path.unlink(missing_ok=True)


class TestHubSpotClient(unittest.TestCase):

    def _make_client(self):
        from gmail_hubspot_sync import HubSpotClient
        return HubSpotClient("test-token")

    def test_init_raises_without_token(self):
        from gmail_hubspot_sync import HubSpotClient
        with self.assertRaises(ValueError):
            HubSpotClient("")

    @patch("gmail_hubspot_sync.requests.post")
    def test_find_contact_returns_none_when_empty(self, mock_post):
        mock_post.return_value.json.return_value = {"results": []}
        mock_post.return_value.raise_for_status = MagicMock()
        client = self._make_client()
        result = client.find_contact_by_email("unknown@example.com")
        self.assertIsNone(result)

    @patch("gmail_hubspot_sync.requests.post")
    def test_find_contact_returns_contact(self, mock_post):
        mock_post.return_value.json.return_value = {
            "results": [{"id": "123", "properties": {"email": "found@example.com"}}]
        }
        mock_post.return_value.raise_for_status = MagicMock()
        client = self._make_client()
        result = client.find_contact_by_email("found@example.com")
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], "123")

    @patch("gmail_hubspot_sync.requests.post")
    def test_create_contact_returns_id(self, mock_post):
        from gmail_hubspot_sync import SenderInfo
        mock_post.return_value.json.return_value = {"id": "999"}
        mock_post.return_value.raise_for_status = MagicMock()
        client = self._make_client()
        sender = SenderInfo(email="new@acme.com", first_name="Anna", last_name="Verdi", company="Acme")
        result = client.create_contact(sender)
        self.assertEqual(result, "999")

    @patch("gmail_hubspot_sync.requests.patch")
    @patch("gmail_hubspot_sync.requests.post")
    def test_update_contact_skips_when_no_changes(self, mock_post, mock_patch):
        from gmail_hubspot_sync import SenderInfo
        client = self._make_client()
        sender = SenderInfo(email="old@acme.com", first_name="Mario", last_name="Rossi", company="Acme")
        existing = {
            "id": "456",
            "properties": {
                "firstname": "Mario",
                "lastname": "Rossi",
                "company": "Acme",
                "lead_source": "Gmail",
            },
        }
        result = client.update_contact("456", sender, existing)
        self.assertFalse(result)
        mock_patch.assert_not_called()


class TestSyncEngine(unittest.TestCase):

    @patch("gmail_hubspot_sync.GmailClient")
    @patch("gmail_hubspot_sync.HubSpotClient")
    def test_run_once_creates_new_contact(self, MockHubSpot, MockGmail):
        from gmail_hubspot_sync import GmailHubSpotSync, SyncState
        import gmail_hubspot_sync as m

        gmail_instance = MockGmail.return_value
        gmail_instance.get_inbox_messages.return_value = [{"id": "msg001"}]
        gmail_instance.get_message_headers.return_value = {
            "From": "Nuovo <nuovo@startup.it>",
            "Subject": "Ciao",
            "Date": "Mon, 26 May 2025 09:00:00 +0000",
            "_internal_date_ms": 1748246400000,
        }

        hs_instance = MockHubSpot.return_value
        hs_instance.find_contact_by_email.return_value = None
        hs_instance.create_contact.return_value = "hs-new-id"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = Path(f.name)

        original_state_file = m.STATE_FILE
        m.STATE_FILE = state_path
        try:
            sync = GmailHubSpotSync.__new__(GmailHubSpotSync)
            sync.gmail = gmail_instance
            sync.hubspot = hs_instance
            sync.state = SyncState()

            results = sync.run_once()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].status, "Creato")
            self.assertEqual(results[0].contact_id, "hs-new-id")
        finally:
            m.STATE_FILE = original_state_file
            state_path.unlink(missing_ok=True)

    @patch("gmail_hubspot_sync.GmailClient")
    @patch("gmail_hubspot_sync.HubSpotClient")
    def test_run_once_skips_duplicate_message(self, MockHubSpot, MockGmail):
        from gmail_hubspot_sync import GmailHubSpotSync, SyncState
        import gmail_hubspot_sync as m

        gmail_instance = MockGmail.return_value
        gmail_instance.get_inbox_messages.return_value = [{"id": "already-seen"}]

        hs_instance = MockHubSpot.return_value

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            state_path = Path(f.name)

        original_state_file = m.STATE_FILE
        m.STATE_FILE = state_path
        try:
            sync = GmailHubSpotSync.__new__(GmailHubSpotSync)
            sync.gmail = gmail_instance
            sync.hubspot = hs_instance
            sync.state = SyncState(processed_message_ids=["already-seen"])

            results = sync.run_once()
            self.assertEqual(results, [])
            gmail_instance.get_message_headers.assert_not_called()
        finally:
            m.STATE_FILE = original_state_file
            state_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
