"""Tests for the email parser module."""

import pytest
from gmail_hubspot_sync.parser import (
    extract_contact,
    is_automated,
    domain_from_email,
    parse_forwarded_sender,
    SyncStatus,
)


def test_is_automated_facebook():
    assert is_automated("notification@priority.facebookmail.com")
    assert is_automated("friends@facebookmail.com")


def test_is_automated_noreply():
    assert is_automated("noreply@service.com")
    assert is_automated("no-reply@example.org")


def test_not_automated_regular():
    assert not is_automated("press@venicemarathon.it")
    assert not is_automated("cristian.mameli@gmail.com")


def test_domain_from_email():
    assert domain_from_email("user@example.com") == "example.com"
    assert domain_from_email("a@b.it") == "b.it"


def test_parse_forwarded_italian_format():
    body = 'Da "carola assumma" carola.assumma@carolaassummacomunicazione.com\nA\nCc\nData ...'
    result = parse_forwarded_sender(body)
    assert result is not None
    name, email = result
    assert email == "carola.assumma@carolaassummacomunicazione.com"
    assert "carola" in name.lower()


def test_parse_forwarded_from_format():
    body = "From: Luca Bramanti <l.bramanti@nextpress.it>\nSubject: Test"
    result = parse_forwarded_sender(body)
    assert result is not None
    name, email = result
    assert email == "l.bramanti@nextpress.it"


def test_extract_contact_direct_sender():
    contact = extract_contact(
        sender="cristian.mameli@gmail.com",
        subject="Test",
        thread_id="abc123",
        body="",
    )
    assert contact is not None
    assert contact.email == "cristian.mameli@gmail.com"
    assert contact.firstname == "Cristian"
    assert contact.lastname == "Mameli"


def test_extract_contact_forwarded():
    body = 'Da "Press Venice Marathon" press@venicemarathon.it\nA redazione@latestata.it'
    contact = extract_contact(
        sender="redazione@latestata.it",
        subject="Fw: Marathon results",
        thread_id="abc456",
        body=body,
    )
    assert contact is not None
    assert contact.email == "press@venicemarathon.it"
    assert contact.company == "Venicemarathon"


def test_extract_contact_automated_returns_none():
    contact = extract_contact(
        sender="notification@priority.facebookmail.com",
        subject="Facebook notification",
        thread_id="fb001",
        body="",
    )
    assert contact is None


def test_extract_contact_generic_domain_no_company():
    contact = extract_contact(
        sender="paolacireddu@gmail.com",
        subject="Ciao",
        thread_id="x1",
        body="",
    )
    assert contact is not None
    assert contact.company == ""
    assert contact.is_generic_domain
