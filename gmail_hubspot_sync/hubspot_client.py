"""HubSpot CRM client — create and update contacts."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"
_CONTACTS_URL = f"{_BASE}/crm/v3/objects/contacts"
_SEARCH_URL = f"{_BASE}/crm/v3/objects/contacts/search"
_NOTES_URL = f"{_BASE}/crm/v3/objects/notes"
_ASSOC_URL = f"{_BASE}/crm/v4/objects/notes/{{note_id}}/associations/contacts/{{contact_id}}"


@dataclass
class HubSpotContact:
    contact_id: str
    email: str
    firstname: Optional[str]
    lastname: Optional[str]
    company: Optional[str]
    lead_source: Optional[str]
    raw: dict


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        self._token = access_token
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[HubSpotContact]:
        """Return existing contact record or None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email.lower()}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company", "lead_source"],
            "limit": 1,
        }
        resp = self._post(_SEARCH_URL, payload)
        results = resp.get("results", [])
        if not results:
            return None
        r = results[0]
        props = r.get("properties", {})
        return HubSpotContact(
            contact_id=r["id"],
            email=props.get("email", ""),
            firstname=props.get("firstname"),
            lastname=props.get("lastname"),
            company=props.get("company"),
            lead_source=props.get("lead_source"),
            raw=r,
        )

    def create_contact(
        self,
        email: str,
        firstname: Optional[str],
        lastname: Optional[str],
        company: Optional[str],
    ) -> str:
        """Create a new contact and return its HubSpot ID."""
        props = self._build_props(email, firstname, lastname, company)
        resp = self._post(_CONTACTS_URL, {"properties": props})
        return resp["id"]

    def update_contact(
        self,
        contact_id: str,
        firstname: Optional[str],
        lastname: Optional[str],
        company: Optional[str],
        existing: HubSpotContact,
    ) -> bool:
        """Fill in missing fields on an existing contact. Returns True if updated."""
        updates: dict[str, str] = {}

        if firstname and not existing.firstname:
            updates["firstname"] = firstname
        if lastname and not existing.lastname:
            updates["lastname"] = lastname
        if company and not existing.company:
            updates["company"] = company
        if not existing.lead_source:
            updates["lead_source"] = "Gmail"

        if not updates:
            return False

        url = f"{_CONTACTS_URL}/{contact_id}"
        self._patch(url, {"properties": updates})
        return True

    # ------------------------------------------------------------------
    # Activity / Note
    # ------------------------------------------------------------------

    def add_inbound_note(
        self, contact_id: str, email: str, subject: str, received_at: str
    ) -> None:
        """Create a CRM note and associate it with the contact."""
        body = (
            f"Inbound Gmail\n"
            f"From: {email}\n"
            f"Subject: {subject}\n"
            f"Received: {received_at}"
        )
        note_payload = {
            "properties": {
                "hs_note_body": body,
                "hs_timestamp": received_at,
            }
        }
        try:
            resp = self._post(_NOTES_URL, note_payload)
            note_id = resp["id"]
            assoc_url = _ASSOC_URL.format(note_id=note_id, contact_id=contact_id)
            self._session.put(
                assoc_url,
                json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
            ).raise_for_status()
        except Exception as e:
            log.warning("Could not create note for contact %s: %s", contact_id, e)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_props(
        email: str,
        firstname: Optional[str],
        lastname: Optional[str],
        company: Optional[str],
    ) -> dict[str, str]:
        props: dict[str, str] = {
            "email": email.lower(),
            "lead_source": "Gmail",
        }
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if company:
            props["company"] = company
        return props

    def _post(self, url: str, payload: dict) -> dict:
        return self._request("POST", url, payload)

    def _patch(self, url: str, payload: dict) -> dict:
        return self._request("PATCH", url, payload)

    def _request(self, method: str, url: str, payload: dict) -> dict:
        for attempt in range(4):
            resp = self._session.request(method, url, json=payload)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("HubSpot rate limit — waiting %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code == 409:
                # Conflict: contact already exists — return the existing one from error body
                data = resp.json()
                msg = data.get("message", "")
                # HubSpot 409 body contains the existing contact ID
                m = re.search(r"existing ID: (\d+)", msg)
                if m:
                    return {"id": m.group(1)}
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"HubSpot API failed after retries: {url}")
