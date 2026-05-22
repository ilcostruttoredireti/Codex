import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from contact_extractor import extract_contact, ContactInfo


def test_full_name_and_email():
    c = extract_contact("Mario Rossi <mario.rossi@example.com>")
    assert c is not None
    assert c.email == "mario.rossi@example.com"
    assert c.first_name == "Mario"
    assert c.last_name == "Rossi"
    assert c.company == "Example"
    assert c.domain == "example.com"


def test_plain_email_no_name():
    c = extract_contact("info@duecubico.it")
    assert c is not None
    assert c.email == "info@duecubico.it"
    assert c.first_name == "Info"
    assert c.company == "Duecubico"


def test_dotted_local_part():
    c = extract_contact("anna.bianchi@latestata.it")
    assert c.first_name == "Anna"
    assert c.last_name == "Bianchi"
    assert c.company == "Latestata"


def test_generic_domain_no_company():
    c = extract_contact("pippo@gmail.com")
    assert c.company == ""


def test_quoted_display_name():
    c = extract_contact('"Ufficio Stampa" <ufficio.stampa@consiglio.regione.lombardia.it>')
    assert c is not None
    assert c.email == "ufficio.stampa@consiglio.regione.lombardia.it"
    assert c.first_name == "Ufficio"
    assert c.last_name == "Stampa"


def test_none_on_empty():
    assert extract_contact("") is None
    assert extract_contact(None) is None


def test_case_normalisation():
    c = extract_contact("TEST@EXAMPLE.COM")
    assert c.email == "test@example.com"
