import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sync.contact_extractor import extract_sender, is_noreply, is_valid_email


def test_full_name_and_company():
    s = extract_sender("Mario Rossi <mario.rossi@acmecorp.com>")
    assert s.email == "mario.rossi@acmecorp.com"
    assert s.first_name == "Mario"
    assert s.last_name == "Rossi"
    assert s.company == "Acmecorp"
    assert s.domain == "acmecorp.com"


def test_personal_domain_no_company():
    s = extract_sender("Luca <luca@gmail.com>")
    assert s.company == ""


def test_email_only_no_display_name():
    s = extract_sender("info@startup.io")
    assert s.email == "info@startup.io"
    assert s.first_name == ""
    assert s.company == "Startup"


def test_noreply_detection():
    assert is_noreply("noreply@example.com")
    assert is_noreply("no-reply@service.io")
    assert is_noreply("bounce@mailserver.com")
    assert not is_noreply("mario@example.com")


def test_invalid_email():
    assert not is_valid_email("")
    assert not is_valid_email("notanemail")
    assert is_valid_email("a@b.com")


def test_case_insensitive():
    s = extract_sender("Test User <Test.User@Company.COM>")
    assert s.email == "test.user@company.com"
