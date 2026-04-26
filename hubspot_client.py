"""HubSpot contacts API — search, create, update."""

import requests
from typing import Optional

CONTACTS_URL = "https://api.hubapi.com/crm/v3/objects/contacts"
SEARCH_URL = "https://api.hubapi.com/crm/v3/objects/contacts/search"
TIMELINE_URL = "https://api.hubapi.com/crm/v3/objects/contacts/{contact_id}/associations/engagements"


class HubSpotClient:
    def __init__(self, api_key: str):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing contact dict or None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            "properties": [
                "email", "firstname", "lastname",
                "company", "hs_lead_status", "lead_source",
            ],
            "limit": 1,
        }
        resp = requests.post(SEARCH_URL, json=payload, headers=self._headers, timeout=15)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, sender: dict) -> dict:
        """Create a new HubSpot contact from *sender* data."""
        company = _company_from_domain(sender["domain"])
        props = {
            "email": sender["email"],
            "lead_source": "Gmail",
        }
        if sender.get("first_name"):
            props["firstname"] = sender["first_name"]
        if sender.get("last_name"):
            props["lastname"] = sender["last_name"]
        if company:
            props["company"] = company

        resp = requests.post(
            CONTACTS_URL,
            json={"properties": props},
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, sender: dict) -> dict:
        """Fill in any blank fields on an existing contact."""
        existing_props = _get_contact_properties(contact_id, self._headers)

        updates = {}

        if not existing_props.get("firstname") and sender.get("first_name"):
            updates["firstname"] = sender["first_name"]
        if not existing_props.get("lastname") and sender.get("last_name"):
            updates["lastname"] = sender["last_name"]
        if not existing_props.get("company"):
            company = _company_from_domain(sender["domain"])
            if company:
                updates["company"] = company
        if not existing_props.get("lead_source"):
            updates["lead_source"] = "Gmail"

        if not updates:
            return {"id": contact_id, "no_changes": True}

        url = f"{CONTACTS_URL}/{contact_id}"
        resp = requests.patch(
            url,
            json={"properties": updates},
            headers=self._headers,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Timeline / activity
    # ------------------------------------------------------------------

    def log_email_received(self, contact_id: str, sender: dict) -> None:
        """Create a 'Email received' note engagement on the contact."""
        note_url = "https://api.hubapi.com/engagements/v1/engagements"
        payload = {
            "engagement": {
                "active": True,
                "type": "NOTE",
            },
            "associations": {
                "contactIds": [int(contact_id)],
            },
            "metadata": {
                "body": (
                    f"Inbound Gmail received from {sender['name'] or sender['email']}"
                    f"\nSubject: {sender.get('subject', '')}"
                    f"\nSource: Inbound Gmail"
                ),
            },
        }
        resp = requests.post(
            note_url,
            json=payload,
            headers=self._headers,
            timeout=15,
        )
        # Non-fatal: just log the error
        if not resp.ok:
            print(f"    [warn] Timeline note failed: {resp.status_code} {resp.text[:120]}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_contact_properties(contact_id: str, headers: dict) -> dict:
    url = f"{CONTACTS_URL}/{contact_id}"
    resp = requests.get(
        url,
        params={"properties": "firstname,lastname,company,lead_source"},
        headers=headers,
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("properties", {})


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from email domain, skipping common providers."""
    free_providers = {
        "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
        "live.com", "icloud.com", "me.com", "aol.com",
        "protonmail.com", "mail.com", "gmx.com",
    }
    if not domain or domain in free_providers:
        return ""
    # Strip TLD → use the SLD as a rough company name
    parts = domain.split(".")
    return parts[-2].capitalize() if len(parts) >= 2 else domain
