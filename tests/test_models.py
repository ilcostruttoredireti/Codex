"""Tests for data models."""

import pytest
from gmail_hubspot_sync.models import ContactInfo, SyncResult, SyncStatus


class TestContactInfo:
    def test_domain_extracted_from_email(self):
        c = ContactInfo(email="mario@example.com")
        assert c.domain == "example.com"

    def test_company_derived_from_domain(self):
        c = ContactInfo(email="mario@acme.com")
        assert c.company == "Acme"

    def test_free_email_no_company(self):
        c = ContactInfo(email="mario@gmail.com")
        assert c.company is None

    def test_explicit_company_not_overridden(self):
        c = ContactInfo(email="mario@acme.com", company="ACME Corp")
        assert c.company == "ACME Corp"

    def test_split_name_two_parts(self):
        c = ContactInfo(email="x@y.com", full_name="Mario Rossi")
        c.split_name()
        assert c.first_name == "Mario"
        assert c.last_name == "Rossi"

    def test_split_name_single(self):
        c = ContactInfo(email="x@y.com", full_name="Mario")
        c.split_name()
        assert c.first_name == "Mario"
        assert c.last_name is None

    def test_split_name_three_parts(self):
        c = ContactInfo(email="x@y.com", full_name="Mario De Rossi")
        c.split_name()
        assert c.first_name == "Mario"
        assert c.last_name == "De Rossi"

    def test_no_name_split_when_already_set(self):
        c = ContactInfo(email="x@y.com", first_name="A", last_name="B", full_name="C D")
        c.split_name()
        assert c.first_name == "A"
        assert c.last_name == "B"


class TestSyncResult:
    def test_str_created(self):
        r = SyncResult(status=SyncStatus.CREATED, email="a@b.com", hubspot_contact_id="123")
        s = str(r)
        assert "Creato" in s
        assert "a@b.com" in s
        assert "123" in s

    def test_str_error(self):
        r = SyncResult(status=SyncStatus.ERROR, email="a@b.com", error="timeout")
        s = str(r)
        assert "Errore" in s
        assert "timeout" in s
