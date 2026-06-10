"""Tests for the sync orchestration logic using mocked API clients."""

from unittest.mock import MagicMock, patch, call
import pytest

from contact_parser import ContactData


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_contact(email, first=None, last=None, company=None):
    return ContactData(email=email, first_name=first, last_name=last, company=company)


def _make_existing(contact_id, props):
    obj = MagicMock()
    obj.id = contact_id
    obj.properties = props
    return obj


# ── sync_contact tests ────────────────────────────────────────────────────────

class TestSyncContact:
    """sync_contact should create, update, or ignore based on HubSpot state."""

    def _run(self, contact, find_return, create_return=None, update_return=False):
        """Run sync_contact with patched hubspot_client functions."""
        import sync as sync_module

        with patch.object(sync_module.hubspot_client, "find_by_email", return_value=find_return), \
             patch.object(sync_module.hubspot_client, "create_contact", return_value=create_return), \
             patch.object(sync_module.hubspot_client, "update_missing_fields", return_value=update_return), \
             patch.object(sync_module, "CREATE_ACTIVITY", False):

            return sync_module.sync_contact(MagicMock(), contact)

    def test_creates_new_contact(self):
        from sync import Status
        c = _make_contact("new@corp.com", first="New", last="User", company="Corp")
        result = self._run(c, find_return=None, create_return="hs-001")
        assert result.status == Status.CREATED
        assert result.contact_id == "hs-001"
        assert result.email == "new@corp.com"

    def test_updates_existing_contact_with_missing_fields(self):
        from sync import Status
        existing = _make_existing("hs-002", {"firstname": "", "lastname": "", "company": ""})
        c = _make_contact("existing@corp.com", first="Jane", last="Doe", company="Corp")
        result = self._run(c, find_return=existing, update_return=True)
        assert result.status == Status.UPDATED
        assert result.contact_id == "hs-002"

    def test_ignores_contact_with_all_fields_filled(self):
        from sync import Status
        existing = _make_existing(
            "hs-003",
            {"firstname": "Jane", "lastname": "Doe", "company": "Corp"},
        )
        c = _make_contact("full@corp.com", first="Jane", last="Doe", company="Corp")
        result = self._run(c, find_return=existing, update_return=False)
        assert result.status == Status.IGNORED

    def test_error_when_create_fails(self):
        from sync import Status
        c = _make_contact("fail@corp.com")
        result = self._run(c, find_return=None, create_return=None)
        assert result.status == Status.ERROR
        assert result.contact_id is None


# ── run_cycle deduplication ───────────────────────────────────────────────────

class TestRunCycleDeduplications:
    """Emails from the same sender within a cycle must only be synced once."""

    def test_same_sender_deduplicated(self):
        import sync as sync_module

        gmail = MagicMock()
        hs = MagicMock()
        seen = set()

        # Two messages from the same sender
        with patch.object(sync_module.gmail_client, "get_inbox_message_ids", return_value=["m1", "m2"]), \
             patch.object(sync_module.gmail_client, "get_from_header", return_value="Alice <alice@corp.com>"), \
             patch.object(sync_module.gmail_client, "get_profile_history_id", return_value="999"), \
             patch.object(sync_module, "sync_contact") as mock_sync, \
             patch.object(sync_module, "MY_EMAIL", ""), \
             patch("state.set_history_id"):

            mock_sync.return_value = MagicMock(status=sync_module.Status.CREATED,
                                               email="alice@corp.com", contact_id="hs-1")
            result_seen = sync_module.run_cycle(gmail, hs, seen)

        # sync_contact should only be called once despite two messages
        assert mock_sync.call_count == 1
        assert "alice@corp.com" in result_seen

    def test_own_email_skipped(self):
        import sync as sync_module

        gmail = MagicMock()
        hs = MagicMock()

        with patch.object(sync_module.gmail_client, "get_inbox_message_ids", return_value=["m1"]), \
             patch.object(sync_module.gmail_client, "get_from_header", return_value="Me <me@example.com>"), \
             patch.object(sync_module.gmail_client, "get_profile_history_id", return_value="999"), \
             patch.object(sync_module, "sync_contact") as mock_sync, \
             patch.object(sync_module, "MY_EMAIL", "me@example.com"), \
             patch("state.set_history_id"):

            sync_module.run_cycle(gmail, hs, set())

        mock_sync.assert_not_called()
