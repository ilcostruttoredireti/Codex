"""
HubSpot CRM client — wraps hubspot-api-client to manage contacts.

Uses a Private App access token (HUBSPOT_API_KEY env var).
"""
from __future__ import annotations

import os
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import SimplePublicUpsertObject

from models import SenderContact

_TOKEN = os.environ.get("HUBSPOT_API_KEY", "")

# HubSpot property names
_PROP_EMAIL = "email"
_PROP_FIRST = "firstname"
_PROP_LAST = "lastname"
_PROP_COMPANY = "company"
_PROP_SOURCE = "hs_lead_status"         # re-used; or a custom property
_PROP_CONTACT_SOURCE = "lead_source_detail"  # custom; may not exist — handled gracefully
_PROP_NOTES = "hs_content_membership_notes"

# Tag we write into a notes/description field
_INBOUND_TAG = "Inbound Gmail"
_SOURCE_VALUE = "Gmail"


def _build_client() -> hubspot.Client:
    if not _TOKEN:
        raise EnvironmentError("HUBSPOT_API_KEY environment variable is not set.")
    return hubspot.Client.create(access_token=_TOKEN)


class HubSpotClient:
    def __init__(self):
        self._hs = _build_client()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing contact dict (id + properties) or None."""
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name=_PROP_EMAIL,
                            operator="EQ",
                            value=email.lower(),
                        )
                    ]
                )
            ],
            properties=[
                _PROP_EMAIL, _PROP_FIRST, _PROP_LAST, _PROP_COMPANY,
            ],
            limit=1,
        )
        try:
            resp = self._hs.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            if resp.total > 0:
                result = resp.results[0]
                return {"id": result.id, "properties": result.properties}
        except ApiException:
            pass
        return None

    # ------------------------------------------------------------------
    # Create / Update
    # ------------------------------------------------------------------

    def _build_properties(self, contact: SenderContact) -> dict:
        props: dict[str, str] = {_PROP_EMAIL: contact.email}
        if contact.first_name:
            props[_PROP_FIRST] = contact.first_name
        if contact.last_name:
            props[_PROP_LAST] = contact.last_name
        if contact.company:
            props[_PROP_COMPANY] = contact.company
        # Source fields — written defensively (unknown custom props are ignored by HubSpot)
        props["leadsource"] = _SOURCE_VALUE
        return props

    def create_contact(self, contact: SenderContact) -> str:
        """Create a new contact and return its HubSpot ID."""
        props = self._build_properties(contact)
        body = SimplePublicObjectInputForCreate(properties=props)
        result = self._hs.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=body
        )
        return result.id

    def update_contact(self, contact_id: str, contact: SenderContact, existing_props: dict) -> None:
        """Update only missing / empty fields on an existing contact."""
        updates: dict[str, str] = {}
        candidate = self._build_properties(contact)
        for key, value in candidate.items():
            if key == _PROP_EMAIL:
                continue  # never overwrite email
            current = existing_props.get(key)
            if not current and value:
                updates[key] = value
        if not updates:
            return
        body = SimplePublicObjectInputForCreate(properties=updates)
        self._hs.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=body,
        )

    # ------------------------------------------------------------------
    # Timeline note (optional)
    # ------------------------------------------------------------------

    def add_email_received_note(
        self,
        contact_id: str,
        subject: str,
        from_email: str,
    ) -> None:
        """
        Create a simple Note engagement associated with the contact to record
        that an inbound email was received.
        """
        try:
            from hubspot.crm.objects.notes import (
                SimplePublicObjectInputForCreate as NoteInput,
            )
            from hubspot.crm.associations import (
                BatchInputPublicAssociation,
                PublicAssociation,
            )

            note_props = {
                "hs_note_body": (
                    f"[{_INBOUND_TAG}] Email ricevuta da {from_email}\n"
                    f"Oggetto: {subject}"
                ),
                "hs_timestamp": str(int(__import__("time").time() * 1000)),
            }
            note = self._hs.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteInput(properties=note_props)
            )
            # Associate note → contact
            self._hs.crm.associations.batch_api.create(
                from_object_type="notes",
                to_object_type="contacts",
                batch_input_public_association=BatchInputPublicAssociation(
                    inputs=[
                        PublicAssociation(
                            from_={"id": note.id},
                            to={"id": contact_id},
                            type="note_to_contact",
                        )
                    ]
                ),
            )
        except Exception:
            # Timeline notes are optional — swallow errors silently
            pass
