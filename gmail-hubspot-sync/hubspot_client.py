"""
HubSpot CRM client — search, create and update contacts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup

HUBSPOT_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")

# Analytics source value for contacts originating from inbound Gmail
GMAIL_SOURCE = "EMAIL_MARKETING"
GMAIL_TAG_NOTE = "Inbound Gmail"


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: str | None


def _client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_TOKEN)


def find_contact_by_email(email: str) -> dict | None:
    """Return existing HubSpot contact dict or None."""
    client = _client()
    req = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(
                filters=[
                    Filter(
                        property_name="email",
                        operator="EQ",
                        value=email,
                    )
                ]
            )
        ],
        properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
        limit=1,
    )
    try:
        res = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        return res.results[0].to_dict() if res.results else None
    except ApiException:
        return None


def create_contact(contact) -> SyncResult:
    """Create a new HubSpot contact from a contact_parser.Contact."""
    client = _client()
    props = {
        "email": contact.email,
        "firstname": contact.firstname,
        "lastname": contact.lastname,
        "company": contact.company,
        "hs_analytics_source": GMAIL_SOURCE,
    }
    # Remove blank values so HubSpot doesn't overwrite with empty strings
    props = {k: v for k, v in props.items() if v}

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties=props
            )
        )
        _add_gmail_note(client, result.id, contact.email)
        return SyncResult(SyncStatus.CREATED, contact.email, result.id)
    except ApiException as e:
        if e.status == 409:
            # Duplicate — treat as existing
            return SyncResult(SyncStatus.IGNORED, contact.email, None)
        raise


def update_contact_if_needed(hubspot_contact: dict, contact) -> SyncResult:
    """Fill in missing fields on an existing contact."""
    existing = hubspot_contact.get("properties", {})
    contact_id = hubspot_contact.get("id")
    updates = {}

    if not existing.get("firstname"):
        updates["firstname"] = contact.firstname
    if not existing.get("lastname") and contact.lastname:
        updates["lastname"] = contact.lastname
    if not existing.get("company") and contact.company:
        updates["company"] = contact.company
    if not existing.get("hs_analytics_source"):
        updates["hs_analytics_source"] = GMAIL_SOURCE

    if not updates:
        return SyncResult(SyncStatus.IGNORED, contact.email, contact_id)

    client = _client()
    client.crm.contacts.basic_api.update(
        contact_id=contact_id,
        simple_public_object_input={"properties": updates},
    )
    return SyncResult(SyncStatus.UPDATED, contact.email, contact_id)


def _add_gmail_note(client: hubspot.Client, contact_id: str, email: str) -> None:
    """Attach a note to the contact recording the Gmail inbound event."""
    try:
        note = client.crm.objects.basic_api.create(
            object_type="notes",
            simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                properties={
                    "hs_note_body": (
                        f"Contatto acquisito da email in arrivo su Gmail.\n"
                        f"Email mittente: {email}\n"
                        f"Tag: {GMAIL_TAG_NOTE}"
                    ),
                    "hs_timestamp": __import__("datetime").datetime.utcnow().isoformat() + "Z",
                }
            ),
        )
        # Associate note with the contact
        client.crm.objects.associations_api.create(
            object_type="notes",
            object_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception:
        pass  # Note creation is best-effort; don't fail the sync
