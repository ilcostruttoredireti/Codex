import pytest
from src.hubspot_client import HubSpotClient
from src.config import PERSONAL_DOMAINS


def test_parse_name_full():
    first, last = HubSpotClient.parse_name("Mario Rossi")
    assert first == "Mario"
    assert last == "Rossi"


def test_parse_name_single_word():
    first, last = HubSpotClient.parse_name("Mario")
    assert first == "Mario"
    assert last == ""


def test_parse_name_multi_word_last():
    first, last = HubSpotClient.parse_name("Jean Pierre Dupont")
    assert first == "Jean"
    assert last == "Pierre Dupont"


def test_company_from_corporate_domain():
    assert HubSpotClient.company_from_domain("acme.com", PERSONAL_DOMAINS) == "Acme"


def test_company_from_personal_domain_returns_none():
    assert HubSpotClient.company_from_domain("gmail.com", PERSONAL_DOMAINS) is None


def test_company_from_libero_returns_none():
    assert HubSpotClient.company_from_domain("libero.it", PERSONAL_DOMAINS) is None


def test_company_capitalisation():
    assert HubSpotClient.company_from_domain("openai.com", PERSONAL_DOMAINS) == "Openai"


def test_company_from_subdomain_uses_first_part():
    assert HubSpotClient.company_from_domain("mail.bigcorp.com", PERSONAL_DOMAINS) == "Mail"
