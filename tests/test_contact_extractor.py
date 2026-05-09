import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
from contact_extractor import extract_contact


def test_full_name_and_company():
    c = extract_contact('Mario Rossi <mario.rossi@acmecorp.com>')
    assert c is not None
    assert c.email == 'mario.rossi@acmecorp.com'
    assert c.first_name == 'Mario'
    assert c.last_name == 'Rossi'
    assert c.company == 'Acmecorp'
    assert c.domain == 'acmecorp.com'


def test_free_email_no_company():
    c = extract_contact('Luca <luca@gmail.com>')
    assert c is not None
    assert c.company is None
    assert c.first_name == 'Luca'


def test_email_only_no_display_name():
    c = extract_contact('noreply@company.io')
    assert c is not None
    assert c.email == 'noreply@company.io'
    assert c.first_name is None
    assert c.last_name is None
    assert c.company == 'Company'


def test_empty_header():
    assert extract_contact('') is None
    assert extract_contact(None) is None


def test_invalid_header():
    assert extract_contact('not-an-email') is None


def test_email_normalised_lowercase():
    c = extract_contact('Test User <Test.User@Corp.COM>')
    assert c.email == 'test.user@corp.com'


def test_hyphenated_domain_company():
    c = extract_contact('a@my-startup.com')
    assert c.company == 'My Startup'


def test_single_name():
    c = extract_contact('Giovanni <g@firma.it>')
    assert c.first_name == 'Giovanni'
    assert c.last_name is None
