import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from hubspot import HubSpot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    PublicObjectSearchRequest,
    ApiException,
)

from config import HUBSPOT_API_KEY

log = logging.getLogger(__name__)


def get_hubspot_client() -> HubSpot:
    return HubSpot(access_token=HUBSPOT_API_KEY)


def find_contact_by_email(client: HubSpot, email: str) -> Optional[dict]:
    """Return the first matching HubSpot contact dict, or None."""
    try:
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            limit=1,
        )
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        if result.total > 0:
            contact = result.results[0]
            return {"id": contact.id, "properties": contact.properties}
        return None
    except ApiException as exc:
        log.error("HubSpot search error for %s: %s", email, exc)
        return None


def create_contact(client: HubSpot, properties: dict) -> Optional[dict]:
    """Create a new contact. Returns {'id': ..., 'properties': ...} or None."""
    try:
        obj = SimplePublicObjectInputForCreate(properties=properties)
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        return {"id": result.id, "properties": result.properties}
    except ApiException as exc:
        log.error("HubSpot create contact error: %s", exc)
        return None


def update_contact(
    client: HubSpot, contact_id: str, properties: dict
) -> Optional[dict]:
    """Update specific properties of an existing contact."""
    try:
        obj = SimplePublicObjectInput(properties=properties)
        result = client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj,
        )
        return {"id": result.id, "properties": result.properties}
    except ApiException as exc:
        log.error("HubSpot update contact %s error: %s", contact_id, exc)
        return None


def add_email_activity(
    client: HubSpot,
    contact_id: str,
    sender_email: str,
    subject: str,
    date_header: str,
) -> None:
    """
    Attach a note to the contact timeline recording the inbound Gmail message.
    Errors are logged but never raised so they don't break the sync flow.
    """
    try:
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteCreate,
        )

        body = (
            f"[Inbound Gmail]\n"
            f"Da: {sender_email}\n"
            f"Oggetto: {subject or '(nessun oggetto)'}\n"
            f"Data: {date_header}\n"
            f"Tag: Inbound Gmail"
        )

        note_input = NoteCreate(
            properties={
                "hs_note_body": body,
                "hs_timestamp": _to_epoch_ms(date_header),
            },
            associations=[
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        )
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_input
        )
    except Exception as exc:
        log.warning("Could not add note activity to contact %s: %s", contact_id, exc)


def _to_epoch_ms(date_str: str) -> str:
    """Convert an RFC 2822 date string to epoch milliseconds (as a string)."""
    try:
        dt = parsedate_to_datetime(date_str)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return str(int(datetime.now(timezone.utc).timestamp() * 1000))
