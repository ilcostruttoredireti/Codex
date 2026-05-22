import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from forward_parser import extract_forwarded_sender, is_forwarded


_IT_BODY = """Da "Ufficio Stampa Consiglio Regionale della Lombardia" ufficio.stampa@consiglio.regione.lombardia.it
A
Cc
Data Thu, 21 May 2026 17:51:13 +0000
Oggetto Lombardia, il Presidente Romani all'anniversario di FLA
"""

_IT_BODY_NO_QUOTES = "Da m.pirollo@duecubico.it A mlg@duecubico.it Cc Data"

_EN_BODY = 'From: "John Doe" <john.doe@example.com>\nTo: someone@else.com'


def test_italian_quoted_name():
    name, email = extract_forwarded_sender(_IT_BODY)
    assert email == "ufficio.stampa@consiglio.regione.lombardia.it"
    assert "Ufficio Stampa" in name


def test_italian_plain_email():
    result = extract_forwarded_sender(_IT_BODY_NO_QUOTES)
    assert result is not None
    name, email = result
    assert email == "m.pirollo@duecubico.it"


def test_english_format():
    name, email = extract_forwarded_sender(_EN_BODY)
    assert email == "john.doe@example.com"
    assert name == "John Doe"


def test_none_on_empty_body():
    assert extract_forwarded_sender("") is None
    assert extract_forwarded_sender("No from header here") is None


def test_is_forwarded():
    assert is_forwarded("Fw: Evento cultural")
    assert is_forwarded("FW:Something")
    assert is_forwarded("Fwd: Test")
    assert not is_forwarded("Re: Test")
    assert not is_forwarded("Normal subject")
