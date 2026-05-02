import re
from typing import Optional

import requests

import config

_BASE = "https://api.hubapi.com"
_CONTACTS = f"{_BASE}/crm/v3/objects/contacts"
_ENGAGEMENTS = f"{_BASE}/crm/v3/objects/emails"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def _raise(resp: requests.Response) -> None:
    if not resp.ok:
        raise RuntimeError(
            f"HubSpot API error {resp.status_code}: {resp.text[:300]}"
        )


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    if not domain or domain in config.SKIP_DOMAINS:
        return ""
    name = domain.split(".")[0]
    return name.capitalize()


def find_contact_by_email(email: str) -> Optional[dict]:
    """Search HubSpot for a contact with the given email. Returns the contact dict or None."""
    resp = requests.post(
        f"{_CONTACTS}/search",
        headers=_headers(),
        json={
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
            "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
            "limit": 1,
        },
    )
    _raise(resp)
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
) -> dict:
    """Create a new HubSpot contact and return the created object."""
    props: dict = {
        "email": email,
        "hs_lead_source": "Gmail",
    }
    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company

    resp = requests.post(_CONTACTS, headers=_headers(), json={"properties": props})
    _raise(resp)
    return resp.json()


def update_contact(contact_id: str, updates: dict) -> dict:
    """Patch a HubSpot contact with the provided property updates."""
    resp = requests.patch(
        f"{_CONTACTS}/{contact_id}",
        headers=_headers(),
        json={"properties": updates},
    )
    _raise(resp)
    return resp.json()


def build_update_payload(existing: dict, first_name: str, last_name: str, company: str) -> dict:
    """Return only properties that are missing from the existing contact."""
    props = existing.get("properties", {})
    updates: dict = {}

    if first_name and not props.get("firstname"):
        updates["firstname"] = first_name
    if last_name and not props.get("lastname"):
        updates["lastname"] = last_name
    if company and not props.get("company"):
        updates["company"] = company
    if not props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    return updates


def add_email_engagement(
    contact_id: str,
    subject: str,
    from_email: str,
    received_at: str,
) -> None:
    """Create an email engagement (timeline activity) on the contact."""
    body = {
        "properties": {
            "hs_timestamp": received_at or "0",
            "hs_email_direction": "INCOMING_EMAIL",
            "hs_email_subject": subject or "(no subject)",
            "hs_email_status": "RECEIVED",
            "hs_email_html": f"<p>Email received from {from_email}</p>",
        },
        "associations": [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 198,
                    }
                ],
            }
        ],
    }
    resp = requests.post(_ENGAGEMENTS, headers=_headers(), json=body)
    # Non-fatal: log but do not raise
    if not resp.ok:
        print(f"  [warn] Could not create engagement: {resp.status_code} {resp.text[:200]}")


def sync_contact(
    email: str,
    first_name: str,
    last_name: str,
    domain: str,
    subject: str,
    date: str,
) -> tuple[str, str]:
    """
    Sync a sender to HubSpot.

    Returns (status, hubspot_contact_id) where status is one of:
    "Creato", "Aggiornato", "Ignorato".
    """
    company = _company_from_domain(domain)
    existing = find_contact_by_email(email)

    if existing is None:
        contact = create_contact(email, first_name, last_name, company)
        contact_id = contact["id"]
        add_email_engagement(contact_id, subject, email, date)
        return "Creato", contact_id

    contact_id = existing["id"]
    updates = build_update_payload(existing, first_name, last_name, company)

    if updates:
        update_contact(contact_id, updates)
        add_email_engagement(contact_id, subject, email, date)
        return "Aggiornato", contact_id

    return "Ignorato", contact_id
