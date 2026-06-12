"""HubSpot CRM client — search, create, and update contacts."""
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import ApiException, SimplePublicObjectInput, PublicObjectSearchRequest
from hubspot.crm.contacts.models import FilterGroup, Filter

from .config import CONTACT_SOURCE, CONTACT_TAG, HUBSPOT_ACCESS_TOKEN


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    note: str = ""


def _build_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=HUBSPOT_ACCESS_TOKEN)


def _search_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Return the existing HubSpot contact dict or None."""
    search_request = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[
                Filter(property_name="email", operator="EQ", value=email)
            ])
        ],
        properties=["firstname", "lastname", "company", "hs_lead_source", "email"],
        limit=1,
    )
    try:
        resp = client.crm.contacts.search_api.do_search(public_object_search_request=search_request)
        if resp.results:
            return resp.results[0]
    except ApiException:
        pass
    return None


def _properties_to_set(
    sender,
    existing: Optional[dict] = None,
) -> dict:
    """Build the property dict, only filling missing fields when updating."""
    existing_props = existing.properties if existing else {}

    props = {}

    # Always set source tag (idempotent)
    props["hs_lead_source"] = CONTACT_SOURCE

    if not existing:
        # New contact — fill everything we have
        props["email"] = sender.email
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company
    else:
        # Existing — only patch genuinely missing fields
        if sender.first_name and not existing_props.get("firstname"):
            props["firstname"] = sender.first_name
        if sender.last_name and not existing_props.get("lastname"):
            props["lastname"] = sender.last_name
        if sender.company and not existing_props.get("company"):
            props["company"] = sender.company

    return props


def upsert_contact(sender) -> SyncResult:
    """Create or update a HubSpot contact. Returns SyncResult."""
    client = _build_client()

    existing = _search_contact_by_email(client, sender.email)

    props = _properties_to_set(sender, existing)

    try:
        if existing is None:
            obj = SimplePublicObjectInput(properties=props)
            created = client.crm.contacts.basic_api.create(simple_public_object_input_for_create=obj)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=sender.email,
                contact_id=created.id,
            )
        else:
            if len(props) <= 1:
                # Only hs_lead_source would change — nothing meaningful to update
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=sender.email,
                    contact_id=existing.id,
                    note="tutti i campi già presenti",
                )
            obj = SimplePublicObjectInput(properties=props)
            client.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=obj,
            )
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=sender.email,
                contact_id=existing.id,
            )
    except ApiException as e:
        return SyncResult(
            status=SyncStatus.IGNORED,
            email=sender.email,
            contact_id=None,
            note=f"HubSpot error: {e.status} {e.reason}",
        )
