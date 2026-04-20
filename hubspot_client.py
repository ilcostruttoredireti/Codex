import time
import logging

import requests

import config

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# Common personal/free email providers — no company name is inferred from these
_PERSONAL_DOMAINS = frozenset(
    {
        "gmail.com", "googlemail.com",
        "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.es",
        "hotmail.com", "hotmail.it", "hotmail.co.uk", "hotmail.fr",
        "outlook.com", "outlook.it",
        "live.com", "live.it",
        "icloud.com", "me.com", "mac.com",
        "aol.com",
        "protonmail.com", "proton.me",
        "libero.it", "virgilio.it", "alice.it", "tin.it", "tiscali.it",
        "fastwebnet.it", "katamail.com",
    }
)


def _headers():
    return {
        "Authorization": f"Bearer {config.HUBSPOT_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }


def search_contact_by_email(email: str) -> dict | None:
    """Return the first HubSpot contact matching *email*, or None."""
    url = f"{_BASE}/crm/v3/objects/contacts/search"
    payload = {
        "filterGroups": [
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        "properties": ["email", "firstname", "lastname", "company", "lifecyclestage"],
        "limit": 1,
    }
    resp = requests.post(url, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    results = resp.json().get("results", [])
    return results[0] if results else None


def create_contact(properties: dict) -> dict:
    """Create a new HubSpot contact and return the created object."""
    url = f"{_BASE}/crm/v3/objects/contacts"
    resp = requests.post(url, json={"properties": properties}, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def update_contact(contact_id: str, properties: dict) -> dict:
    """Patch an existing contact by ID and return the updated object."""
    url = f"{_BASE}/crm/v3/objects/contacts/{contact_id}"
    resp = requests.patch(url, json={"properties": properties}, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def create_note(contact_id: str, body: str, timestamp_ms: int | None = None) -> dict | None:
    """
    Create a HubSpot Note associated with *contact_id* (best-effort).
    Failures are logged at DEBUG level and do not propagate.
    """
    url = f"{_BASE}/crm/v3/objects/notes"
    ts = str(timestamp_ms or int(time.time() * 1000))
    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": ts,
        },
        "associations": [
            {
                "to": {"id": str(contact_id)},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }
                ],
            }
        ],
    }
    try:
        resp = requests.post(url, json=payload, headers=_headers(), timeout=15)
        if not resp.ok:
            logger.debug("Note creation %s: %s", resp.status_code, resp.text[:200])
            return None
        return resp.json()
    except Exception as exc:
        logger.debug("Note creation failed (non-critical): %s", exc)
        return None


def company_from_domain(domain: str | None) -> str | None:
    """
    Infer a company display name from an email domain.
    Returns None for personal/free domains or when domain is absent.
    Example: 'acme.com' → 'Acme', 'mail.acme.co.uk' → 'Acme'
    """
    if not domain or domain.lower() in _PERSONAL_DOMAINS:
        return None
    parts = domain.split(".")
    # For 'sub.company.tld' take the second-to-last segment; for 'company.tld' take the first.
    segment = parts[-2] if len(parts) >= 2 else parts[0]
    return segment.capitalize()


def split_full_name(full_name: str | None) -> tuple[str | None, str | None]:
    """Split 'First Last …' into (first, rest). Returns (None, None) for empty input."""
    if not full_name:
        return None, None
    parts = full_name.strip().split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else None)
