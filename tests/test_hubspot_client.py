"""Unit tests for HubSpotClient using mocked HTTP responses."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import MagicMock, patch
from hubspot_client import HubSpotClient, ContactData


def _make_client():
    return HubSpotClient(access_token="test-token")


def _mock_response(status_code: int, json_data: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    resp.raise_for_status = MagicMock()
    return resp


def test_create_new_contact():
    client = _make_client()
    contact = ContactData(email="new@acme.com", first_name="Mario", last_name="Rossi", company="Acme")

    search_resp = _mock_response(200, {"results": []})
    create_resp = _mock_response(201, {"id": "12345"})

    with patch.object(client, "_post", side_effect=[search_resp, create_resp]):
        result = client.upsert_contact(contact)

    assert result.status == "created"
    assert result.contact_id == "12345"
    assert result.email == "new@acme.com"


def test_update_existing_contact_with_missing_fields():
    client = _make_client()
    contact = ContactData(email="existing@acme.com", first_name="Luigi", last_name="Verdi", company="Acme")

    existing = {
        "id": "99",
        "properties": {"email": "existing@acme.com", "firstname": "", "lastname": "", "company": ""},
    }
    search_resp = _mock_response(200, {"results": [existing]})
    patch_resp = _mock_response(200, {"id": "99"})

    with patch.object(client, "_post", return_value=search_resp), \
         patch.object(client, "_patch", return_value=patch_resp):
        result = client.upsert_contact(contact)

    assert result.status == "updated"
    assert result.contact_id == "99"


def test_ignore_when_no_new_fields():
    client = _make_client()
    contact = ContactData(email="full@acme.com", first_name="Anna", last_name="Bianchi", company="Acme")

    existing = {
        "id": "77",
        "properties": {
            "email": "full@acme.com",
            "firstname": "Anna",
            "lastname": "Bianchi",
            "company": "Acme",
        },
    }
    search_resp = _mock_response(200, {"results": [existing]})

    with patch.object(client, "_post", return_value=search_resp):
        result = client.upsert_contact(contact)

    assert result.status == "ignored"
    assert result.contact_id == "77"
