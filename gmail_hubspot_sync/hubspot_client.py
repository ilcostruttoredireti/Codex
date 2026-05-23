"""HubSpot API client: contact lookup, creation, and update."""

from dataclasses import dataclass
from enum import Enum

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicUpsertObject

from config import Config
from gmail_client import SenderInfo


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str
    message_id: str


def _build_client() -> hubspot.Client:
    return hubspot.Client.create(access_token=Config.HUBSPOT_ACCESS_TOKEN)


def _search_contact(client: hubspot.Client, email: str) -> dict | None:
    """Return the existing HubSpot contact dict or None."""
    request = PublicObjectSearchRequest(
        filter_groups=[
            {
                "filters": [
                    {"propertyName": "email", "operator": "EQ", "value": email}
                ]
            }
        ],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )
    response = client.crm.contacts.search_api.do_search(
        public_object_search_request=request
    )
    if response.total > 0:
        return response.results[0]
    return None


def _build_properties(sender: SenderInfo, existing: dict | None) -> dict:
    """Build the HubSpot properties dict, filling only missing or empty fields."""
    props: dict = {
        "email": sender.email,
        "hs_lead_source": Config.CONTACT_SOURCE,
    }

    existing_props = existing.properties if existing else {}

    # firstname / lastname — only set if not already present
    if not existing_props.get("firstname") and sender.first_name:
        props["firstname"] = sender.first_name
    if not existing_props.get("lastname") and sender.last_name:
        props["lastname"] = sender.last_name

    # company — set from domain heuristic if missing
    if not existing_props.get("company") and sender.company:
        props["company"] = sender.company

    return props


def sync_contact(sender: SenderInfo) -> SyncResult:
    """Create or update a HubSpot contact from a Gmail sender."""
    client = _build_client()

    try:
        existing = _search_contact(client, sender.email)

        if existing:
            props = _build_properties(sender, existing)
            # Remove email from update payload (it's the key, not a patch field)
            props.pop("email", None)

            if not props:
                return SyncResult(
                    status=SyncStatus.SKIPPED,
                    email=sender.email,
                    contact_id=existing.id,
                    message_id=sender.message_id,
                )

            client.crm.contacts.basic_api.update(
                contact_id=existing.id,
                simple_public_object_input=SimplePublicObjectInputForCreate(
                    properties=props
                ),
            )

            # Add "Inbound Gmail" tag via note/list — HubSpot doesn't have native tags,
            # so we use a custom property if it exists, otherwise skip gracefully.
            _try_add_tag(client, existing.id)

            return SyncResult(
                status=SyncStatus.UPDATED,
                email=sender.email,
                contact_id=existing.id,
                message_id=sender.message_id,
            )

        else:
            props = _build_properties(sender, None)
            new_contact = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )

            _try_add_tag(client, new_contact.id)
            _try_log_activity(client, new_contact.id, sender)

            return SyncResult(
                status=SyncStatus.CREATED,
                email=sender.email,
                contact_id=new_contact.id,
                message_id=sender.message_id,
            )

    except ApiException as exc:
        # Conflict (409) means a duplicate slipped through the search window
        if exc.status == 409:
            existing = _search_contact(client, sender.email)
            contact_id = existing.id if existing else "unknown"
            return SyncResult(
                status=SyncStatus.SKIPPED,
                email=sender.email,
                contact_id=contact_id,
                message_id=sender.message_id,
            )
        raise


def _try_add_tag(client: hubspot.Client, contact_id: str) -> None:
    """Attempt to set the custom 'inbound_gmail' property if it exists."""
    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=SimplePublicObjectInputForCreate(
                properties={"hs_tag": Config.CONTACT_TAG}
            ),
        )
    except Exception:
        # Property may not exist in this HubSpot portal — silently skip
        pass


def _try_log_activity(client: hubspot.Client, contact_id: str, sender: SenderInfo) -> None:
    """Log a 'Email received' engagement on the contact timeline."""
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteInput,
        )
        from hubspot.crm.associations.v4 import (
            AssociationSpec,
        )

        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInput(
                properties={
                    "hs_note_body": (
                        f"Email ricevuta da {sender.display_name} &lt;{sender.email}&gt;.\n"
                        f"Fonte: {Config.CONTACT_SOURCE}"
                    ),
                    "hs_timestamp": str(int(__import__("time").time() * 1000)),
                }
            )
        )

        client.crm.objects.notes.associations_api.create(
            note_id=note.id,
            to_object_type="contacts",
            to_object_id=contact_id,
            association_type="note_to_contact",
        )
    except Exception:
        # Engagements API may need extra scopes — skip silently
        pass
