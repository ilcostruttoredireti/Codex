import time
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate as ContactCreate,
    SimplePublicObjectInput as ContactUpdate,
    PublicObjectSearchRequest,
    ApiException,
)

from config import PERSONAL_DOMAINS


def get_client(access_token: str) -> hubspot.Client:
    return hubspot.Client.create(access_token=access_token)


# ---------------------------------------------------------------------------
# Contact search
# ---------------------------------------------------------------------------

def find_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """
    Return {'id': str, 'properties': dict} for an existing contact, or None.
    """
    try:
        req = PublicObjectSearchRequest(
            filter_groups=[{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            limit=1,
        )
        result = client.crm.contacts.search_api.do_search(
            public_object_search_request=req
        )
        if result.total > 0:
            c = result.results[0]
            return {"id": c.id, "properties": c.properties}
        return None
    except ApiException as exc:
        print(f"    [hubspot] Search error: {exc.status} {exc.reason}")
        return None


# ---------------------------------------------------------------------------
# Company name helper
# ---------------------------------------------------------------------------

def _company_from_domain(domain: str) -> str:
    """
    Infer a company name from an email domain.
    Returns '' for personal/free-mail domains.
    """
    if not domain or domain in PERSONAL_DOMAINS:
        return ""
    parts = domain.split(".")
    # Use second-to-last segment, capitalised (e.g. 'acme' from 'mail.acme.com')
    name = parts[-2] if len(parts) >= 2 else parts[0]
    return name.capitalize()


# ---------------------------------------------------------------------------
# Create contact
# ---------------------------------------------------------------------------

def create_contact(client: hubspot.Client, sender: dict) -> Optional[str]:
    """Create a new HubSpot contact. Returns the new contact ID or None."""
    props: dict = {
        "email": sender["email"],
        "hs_lead_source": "Gmail",
    }
    if sender.get("firstname"):
        props["firstname"] = sender["firstname"]
    if sender.get("lastname"):
        props["lastname"] = sender["lastname"]
    company = _company_from_domain(sender.get("domain", ""))
    if company:
        props["company"] = company

    try:
        result = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=ContactCreate(properties=props)
        )
        return result.id
    except ApiException as exc:
        print(f"    [hubspot] Create error: {exc.status} {exc.reason}")
        return None


# ---------------------------------------------------------------------------
# Update contact (fill missing fields only)
# ---------------------------------------------------------------------------

def update_contact(
    client: hubspot.Client,
    contact_id: str,
    existing_props: dict,
    sender: dict,
) -> bool:
    """
    Patch a contact with any fields that are currently blank.
    Returns True if at least one field was updated.
    """
    updates: dict = {}

    if not existing_props.get("firstname") and sender.get("firstname"):
        updates["firstname"] = sender["firstname"]
    if not existing_props.get("lastname") and sender.get("lastname"):
        updates["lastname"] = sender["lastname"]
    if not existing_props.get("company"):
        company = _company_from_domain(sender.get("domain", ""))
        if company:
            updates["company"] = company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    if not updates:
        return False

    try:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=ContactUpdate(properties=updates),
        )
        return True
    except ApiException as exc:
        print(f"    [hubspot] Update error: {exc.status} {exc.reason}")
        return False


# ---------------------------------------------------------------------------
# Timeline note
# ---------------------------------------------------------------------------

def create_email_note(
    client: hubspot.Client,
    contact_id: str,
    sender: dict,
) -> Optional[str]:
    """
    Create a HubSpot Note engagement associated with the contact,
    recording that an inbound Gmail was received.
    """
    display_name = sender.get("name") or sender["email"]
    body = (
        f"📧 Inbound Gmail received\n\n"
        f"From:    {display_name} <{sender['email']}>\n"
        f"Subject: {sender.get('subject') or '(no subject)'}\n"
        f"Date:    {sender.get('date') or 'unknown'}\n\n"
        f"Tag: Inbound Gmail\n"
        f"Source: Gmail"
    )

    try:
        from hubspot.crm.objects import SimplePublicObjectInputForCreate as ObjCreate

        note = ObjCreate(
            properties={
                "hs_note_body": body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            associations=[
                {
                    "to": {"id": str(contact_id)},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,   # note → contact
                        }
                    ],
                }
            ],
        )
        result = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note
        )
        return result.id
    except Exception as exc:
        # Note creation is optional; log but don't fail the sync
        print(f"    [hubspot] Note creation skipped: {exc}")
        return None
