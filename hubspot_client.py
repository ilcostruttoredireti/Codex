from __future__ import annotations

import time

from hubspot import HubSpot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)


class HubSpotClient:
    def __init__(self, access_token: str) -> None:
        self._hs = HubSpot(access_token=access_token)

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> object | None:
        """Return the HubSpot contact object for *email*, or ``None`` if absent."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            properties=["email", "firstname", "lastname", "company", "lead_source"],
            limit=1,
        )
        try:
            response = self._hs.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0]
        except ApiException as exc:
            print(f"[HubSpot] Search error for {email}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Contact creation / update
    # ------------------------------------------------------------------

    def create_contact(self, contact: dict) -> object | None:
        """Create a new HubSpot contact and return the created object."""
        props = self._build_props(contact, is_new=True)
        try:
            return self._hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
        except ApiException as exc:
            # 409 Conflict means the contact exists under a different record
            if exc.status == 409:
                return None
            print(f"[HubSpot] Create error for {contact['email']}: {exc}")
            return None

    def update_contact(
        self, contact_id: str, contact: dict, existing: object
    ) -> object | None:
        """Patch an existing contact with any fields that are currently blank."""
        existing_props: dict = (
            existing.properties if hasattr(existing, "properties") else {}
        )
        updates: dict[str, str] = {}

        if not existing_props.get("firstname") and contact.get("first_name"):
            updates["firstname"] = contact["first_name"]
        if not existing_props.get("lastname") and contact.get("last_name"):
            updates["lastname"] = contact["last_name"]
        if not existing_props.get("company") and contact.get("company"):
            updates["company"] = contact["company"]
        # Stamp source only when missing
        if not existing_props.get("lead_source"):
            updates["lead_source"] = "Gmail"

        if not updates:
            return existing

        try:
            return self._hs.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=updates
                ),
            )
        except ApiException as exc:
            print(f"[HubSpot] Update error for contact {contact_id}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Activity timeline
    # ------------------------------------------------------------------

    def add_email_activity(
        self, contact_id: str, sender_email: str, subject: str = ""
    ) -> None:
        """Attach a note to *contact_id* recording the inbound email event."""
        body = f"Inbound Gmail email received from {sender_email}"
        if subject:
            body += f"\nSubject: {subject}"
        body += "\nTag: Inbound Gmail"

        timestamp_ms = str(int(time.time() * 1000))
        try:
            from hubspot.crm.objects.notes import (  # lazy import – optional dep path
                SimplePublicObjectInputForCreate as NoteCreate,
            )

            note = self._hs.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteCreate(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": timestamp_ms,
                    }
                )
            )
            self._associate_note(note.id, contact_id)
        except Exception as exc:
            # Activity logging is best-effort; never block the main sync
            print(f"[HubSpot] Could not add activity for contact {contact_id}: {exc}")

    def _associate_note(self, note_id: str, contact_id: str) -> None:
        """Link a note object to a contact via the v4 associations API."""
        try:
            self._hs.crm.associations.v4.basic_api.create(
                object_type="notes",
                object_id=note_id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_spec=[
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        # 202 = note-to-contact (standard HubSpot type id)
                        "associationTypeId": 202,
                    }
                ],
            )
        except Exception as exc:
            print(f"[HubSpot] Association error note={note_id} contact={contact_id}: {exc}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_props(contact: dict, *, is_new: bool = False) -> dict[str, str]:
        props: dict[str, str] = {"email": contact["email"]}
        if contact.get("first_name"):
            props["firstname"] = contact["first_name"]
        if contact.get("last_name"):
            props["lastname"] = contact["last_name"]
        if contact.get("company"):
            props["company"] = contact["company"]
        if is_new:
            props["lead_source"] = "Gmail"
        return props
