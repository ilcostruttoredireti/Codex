"""Unit tests for gmail_hubspot_sync.py"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from gmail_hubspot_sync import (
    should_skip,
    extract_contact,
    process_threads,
    build_hubspot_properties,
    determine_status,
    SenderContact,
)


class TestShouldSkip:
    def test_noreply_skipped(self):
        assert should_skip("noreply@example.com")
        assert should_skip("no-reply@service.com")
        assert should_skip("notifications-noreply@linkedin.com")

    def test_real_person_not_skipped(self):
        assert not should_skip("riccardo@martes-ai.com")
        assert not should_skip("gabriele@ag.miraconsulting.it")

    def test_google_domain_skipped(self):
        assert should_skip("something@google.com")

    def test_tiktok_support_skipped(self):
        assert should_skip("sellersupport@shop.tiktok.com")

    def test_nobody_skipped(self):
        assert should_skip("nobody@e.feedspot.com")


class TestExtractContact:
    def test_name_email_format(self):
        c = extract_contact("Riccardo Belli <riccardo@martes-ai.com>")
        assert c is not None
        assert c.email == "riccardo@martes-ai.com"
        assert c.first_name == "Riccardo"
        assert c.last_name == "Belli"
        assert c.domain == "martes-ai.com"

    def test_email_only(self):
        c = extract_contact("giuseppe@rec-media.it")
        assert c is not None
        assert c.email == "giuseppe@rec-media.it"
        assert c.first_name == "Giuseppe"

    def test_noreply_returns_none(self):
        assert extract_contact("noreply@example.com") is None

    def test_company_derived_from_domain(self):
        c = extract_contact("info@eskimoz.it")
        assert c is not None
        assert c.company == "Eskimoz"


class TestProcessThreads:
    def test_deduplication(self):
        threads = [
            {"messages": [{"sender": "riccardo@martes-ai.com"}]},
            {"messages": [{"sender": "riccardo@martes-ai.com"}]},
        ]
        result = process_threads(threads)
        assert len(result) == 1

    def test_skips_automated(self):
        threads = [
            {"messages": [{"sender": "noreply@example.com"}]},
            {"messages": [{"sender": "real@company.com"}]},
        ]
        result = process_threads(threads)
        assert len(result) == 1
        assert result[0]["email"] == "real@company.com"

    def test_multiple_messages_per_thread(self):
        threads = [
            {
                "messages": [
                    {"sender": "alice@company.com"},
                    {"sender": "bob@other.com"},
                ]
            }
        ]
        result = process_threads(threads)
        emails = {r["email"] for r in result}
        assert emails == {"alice@company.com", "bob@other.com"}


class TestBuildHubspotProperties:
    def test_all_fields_populated_when_none_exist(self):
        c = SenderContact(
            email="test@example.com",
            first_name="Test",
            last_name="User",
            company="Example",
        )
        props = build_hubspot_properties(c, existing={})
        assert props["email"] == "test@example.com"
        assert props["firstname"] == "Test"
        assert props["lastname"] == "User"
        assert props["company"] == "Example"
        assert props["hs_lead_source"] == "Gmail"

    def test_existing_fields_not_overwritten(self):
        c = SenderContact(email="test@example.com", first_name="Test", company="Example")
        existing = {"firstname": "Already Set", "company": "Existing Co"}
        props = build_hubspot_properties(c, existing=existing)
        assert "firstname" not in props
        assert "company" not in props

    def test_lead_source_set_when_missing(self):
        c = SenderContact(email="x@y.com")
        props = build_hubspot_properties(c, existing={})
        assert props.get("hs_lead_source") == "Gmail"


class TestDetermineStatus:
    def test_new_contact_is_creato(self):
        assert determine_status(None, {"email": "x"}) == "Creato"

    def test_existing_with_updates_is_aggiornato(self):
        assert determine_status("123", {"firstname": "Test"}) == "Aggiornato"

    def test_existing_no_updates_is_ignorato(self):
        assert determine_status("123", {}) == "Ignorato"
