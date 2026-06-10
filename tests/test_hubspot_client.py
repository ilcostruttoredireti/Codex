from unittest.mock import MagicMock, patch
from gmail_hubspot_sync.contact_parser import ContactData
from gmail_hubspot_sync.hubspot_client import (
    HubSpotClient,
    SyncResult,
    _build_create_properties,
    _build_update_properties,
)


def _make_contact(email="test@acme.com", first="Mario", last="Rossi", company="Acme"):
    return ContactData(
        email=email,
        first_name=first,
        last_name=last,
        company=company,
        domain=email.split("@")[1],
    )


# --- _build_create_properties ---

def test_build_create_all_fields():
    cd = _make_contact()
    props = _build_create_properties(cd)
    assert props["email"] == "test@acme.com"
    assert props["firstname"] == "Mario"
    assert props["lastname"] == "Rossi"
    assert props["company"] == "Acme"
    assert props["leadsource"] == "Gmail"


def test_build_create_no_company():
    cd = _make_contact(email="user@gmail.com", company=None)
    props = _build_create_properties(cd)
    assert "company" not in props
    assert props["leadsource"] == "Gmail"


# --- _build_update_properties ---

def test_update_fills_missing_fields():
    cd = _make_contact()
    existing = {"firstname": "", "lastname": None, "company": None, "leadsource": None}
    props = _build_update_properties(cd, existing)
    assert "firstname" in props
    assert "lastname" in props
    assert "company" in props
    assert "leadsource" in props


def test_update_skips_existing_fields():
    cd = _make_contact()
    existing = {
        "firstname": "Existing",
        "lastname": "Name",
        "company": "OldCorp",
        "leadsource": "OTHER",
    }
    props = _build_update_properties(cd, existing)
    assert props == {}


def test_update_partial_fill():
    cd = _make_contact(company="NewCorp")
    existing = {"firstname": "Mario", "lastname": "Rossi", "company": None, "leadsource": "Gmail"}
    props = _build_update_properties(cd, existing)
    assert props == {"company": "NewCorp"}


# --- Integration-style tests with mocked HubSpot client ---

@patch("gmail_hubspot_sync.hubspot_client.hubspot.Client.create")
def test_create_contact_returns_created(mock_hs_create):
    mock_client = MagicMock()
    mock_hs_create.return_value = mock_client
    mock_client.crm.contacts.basic_api.create.return_value = MagicMock(id="123")

    client = HubSpotClient(access_token="fake-token")
    cd = _make_contact()
    outcome = client.create_contact(cd)

    assert outcome.result == SyncResult.CREATED
    assert outcome.contact_id == "123"
    assert outcome.contact_email == "test@acme.com"


@patch("gmail_hubspot_sync.hubspot_client.hubspot.Client.create")
def test_create_contact_api_error_returns_skipped(mock_hs_create):
    from hubspot.crm.contacts import ApiException

    mock_client = MagicMock()
    mock_hs_create.return_value = mock_client
    mock_client.crm.contacts.basic_api.create.side_effect = ApiException(status=400)

    client = HubSpotClient(access_token="fake-token")
    outcome = client.create_contact(_make_contact())

    assert outcome.result == SyncResult.SKIPPED
    assert outcome.contact_id is None
