"""
HubSpot CRM API client — contact search, create, update, and timeline notes.
All HTTP calls use requests with the private-app Bearer token.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

import requests

import config

log = logging.getLogger(__name__)

# ── HTTP session ───────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update({"Content-Type": "application/json"})


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {config.HUBSPOT_TOKEN}"}


def _get(path: str, **kwargs) -> dict:
    resp = _session.get(
        f"{config.HUBSPOT_BASE_URL}{path}",
        headers=_auth_headers(),
        **kwargs,
    )
    resp.raise_for_status()
    return resp.json()


def _post(path: str, payload: dict) -> dict:
    resp = _session.post(
        f"{config.HUBSPOT_BASE_URL}{path}",
        headers=_auth_headers(),
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


def _patch(path: str, payload: dict) -> dict:
    resp = _session.patch(
        f"{config.HUBSPOT_BASE_URL}{path}",
        headers=_auth_headers(),
        json=payload,
    )
    resp.raise_for_status()
    return resp.json()


# ── Contact search ─────────────────────────────────────────────────────────────

def find_contact_by_email(email: str) -> Optional[dict]:
    """
    Return the first HubSpot contact whose email matches, or None.
    Returned dict has keys: id, properties (firstname, lastname, company, hs_lead_status).
    """
    payload = {
        "filterGroups": [{
            "filters": [{
                "propertyName": "email",
                "operator": "EQ",
                "value": email,
            }]
        }],
        "properties": [
            "email", "firstname", "lastname",
            "company", "hs_lead_status", "lifecyclestage",
        ],
        "limit": 1,
    }
    result = _post("/crm/v3/objects/contacts/search", payload)
    hits = result.get("results", [])
    return hits[0] if hits else None


# ── Name & company helpers ─────────────────────────────────────────────────────

def _split_name(full_name: Optional[str]) -> tuple[str, str]:
    if not full_name:
        return "", ""
    parts = full_name.strip().split(None, 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from the email domain."""
    if domain in config.PERSONAL_DOMAINS:
        return ""
    # Strip TLD(s) and capitalise
    stem = domain.split(".")[0]
    return stem.replace("-", " ").replace("_", " ").title()


def _build_properties(sender: dict, existing_props: Optional[dict] = None) -> dict:
    """
    Build a properties dict for create or update.
    When *existing_props* is provided, only include fields that are currently empty.
    """
    firstname, lastname = _split_name(sender.get("name"))
    company = _company_from_domain(sender["domain"])
    ep = existing_props or {}

    props: dict = {}

    def _set_if_missing(key: str, value: str):
        if value and not ep.get(key):
            props[key] = value

    if existing_props is None:
        # Creating — set everything we have
        props["email"] = sender["email"]
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if company:
            props["company"] = company
        props["hs_lead_status"] = "NEW"
        props["lifecyclestage"] = "lead"
    else:
        # Updating — only fill gaps
        _set_if_missing("firstname", firstname)
        _set_if_missing("lastname", lastname)
        _set_if_missing("company", company)

    return props


# ── Create / Update ────────────────────────────────────────────────────────────

def create_contact(sender: dict) -> dict:
    """Create a new HubSpot contact and attach a timeline note. Returns the contact dict."""
    props = _build_properties(sender)
    contact = _post("/crm/v3/objects/contacts", {"properties": props})
    log.debug("Created contact %s (%s)", contact["id"], sender["email"])
    _add_timeline_note(contact["id"], sender)
    return contact


def update_contact(contact_id: str, sender: dict, existing: dict) -> tuple[dict, bool]:
    """
    Patch missing fields on an existing contact.
    Returns (contact_dict, was_changed).
    """
    updates = _build_properties(sender, existing_props=existing.get("properties", {}))

    if not updates:
        log.debug("No updates needed for contact %s", contact_id)
        return existing, False

    updated = _patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})
    log.debug("Updated contact %s — fields: %s", contact_id, list(updates))
    return updated, True


# ── Timeline note ──────────────────────────────────────────────────────────────

def _add_timeline_note(contact_id: str, sender: dict):
    """Attach an activity note to the contact's timeline."""
    timestamp_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    body = (
        f"📧 Email ricevuta — Gmail Sync\n"
        f"Da: {sender.get('name') or sender['email']}\n"
        f"Oggetto: {sender.get('subject') or '(nessun oggetto)'}\n"
        f"Data: {sender.get('date') or 'N/A'}\n\n"
        f"Tag: Inbound Gmail\n"
        f"Fonte contatto: Gmail"
    )

    payload = {
        "properties": {
            "hs_note_body": body,
            "hs_timestamp": str(timestamp_ms),
        },
        "associations": [{
            "to": {"id": int(contact_id)},
            "types": [{
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": config.NOTE_TO_CONTACT_ASSOC_TYPE_ID,
            }],
        }],
    }

    try:
        _post("/crm/v3/objects/notes", payload)
        log.debug("Timeline note added to contact %s", contact_id)
    except requests.HTTPError as exc:
        log.warning("Could not add note to contact %s: %s", contact_id, exc)
