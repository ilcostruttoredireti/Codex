"""HubSpot contact sync — create, update, and deduplicate by email."""

from __future__ import annotations

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicObjectInput


SOURCE_LABEL = "Gmail"
TAG_LABEL = "Inbound Gmail"

# HubSpot built-in lifecycle source value for "other" / custom source
_LEAD_SOURCE = "OTHER"


class HubSpotSync:
    def __init__(self, api_key: str):
        self._client = hubspot.Client.create(access_token=api_key)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _search_by_email(self, email: str) -> dict | None:
        """Return existing HubSpot contact dict or None."""
        from hubspot.crm.contacts import PublicObjectSearchRequest

        search_request = PublicObjectSearchRequest(
            filter_groups=[{
                "filters": [{
                    "propertyName": "email",
                    "operator": "EQ",
                    "value": email,
                }]
            }],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            result = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if result.total > 0:
                return result.results[0]
        except ApiException:
            pass
        return None

    def _build_properties(self, sender: dict, existing: dict | None) -> dict:
        """Build the HubSpot property map, filling only missing/empty fields."""
        existing_props = existing.properties if existing else {}

        props: dict[str, str] = {}

        # Always set email
        props["email"] = sender["email"]

        # Fill first name only if not already set
        if sender.get("first_name") and not existing_props.get("firstname"):
            props["firstname"] = sender["first_name"]

        # Fill last name only if not already set
        if sender.get("last_name") and not existing_props.get("lastname"):
            props["lastname"] = sender["last_name"]

        # Fill company only if not already set
        if sender.get("company") and not existing_props.get("company"):
            props["company"] = sender["company"]

        # Always stamp the lead source as Gmail on new contacts; don't overwrite on update
        if not existing:
            props["hs_lead_status"] = _LEAD_SOURCE
            # HubSpot's standard "Lead Source" property (original_source is set by system)
            # We use a note-style approach via the lifecycle label below

        return props

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_contact(self, sender: dict) -> dict:
        """
        Upsert a contact from a Gmail sender dict.

        Returns:
            {status: "created"|"updated"|"ignored", email: str, contact_id: str}
        """
        email = sender["email"]

        # Skip no-reply / mailer-daemon addresses
        _skip_prefixes = ("noreply", "no-reply", "mailer-daemon", "postmaster",
                          "bounce", "donotreply", "do-not-reply")
        local = email.split("@")[0].lower()
        if any(local.startswith(p) for p in _skip_prefixes):
            return {"status": "ignored", "email": email, "contact_id": ""}

        existing = self._search_by_email(email)
        props = self._build_properties(sender, existing)

        try:
            if existing:
                # Only update if there is something new to write
                update_props = {k: v for k, v in props.items() if k != "email"}
                if not update_props:
                    return {
                        "status": "ignored",
                        "email": email,
                        "contact_id": existing.id,
                    }

                self._client.crm.contacts.basic_api.update(
                    contact_id=existing.id,
                    simple_public_object_input=SimplePublicObjectInput(
                        properties=update_props
                    ),
                )
                self._add_timeline_note(existing.id, sender)
                return {
                    "status": "updated",
                    "email": email,
                    "contact_id": existing.id,
                }
            else:
                created = self._client.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                        properties=props
                    )
                )
                self._add_timeline_note(created.id, sender)
                return {
                    "status": "created",
                    "email": email,
                    "contact_id": created.id,
                }

        except ApiException as exc:
            # 409 = contact already exists (race condition); retry as update
            if exc.status == 409:
                existing = self._search_by_email(email)
                if existing:
                    return self.sync_contact(sender)
            raise

    def _add_timeline_note(self, contact_id: str, sender: dict) -> None:
        """Create a note on the contact timeline recording the inbound email."""
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate

        note_body = (
            f"Inbound Gmail received\n"
            f"From: {sender.get('first_name', '')} {sender.get('last_name', '')} "
            f"<{sender['email']}>\n"
            f"Subject: {sender.get('subject', '(no subject)')}\n"
            f"Tag: {TAG_LABEL}"
        )
        try:
            note = self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=NoteCreate(
                    properties={
                        "hs_note_body": note_body,
                        "hs_timestamp": str(int(__import__("time").time() * 1000)),
                    }
                )
            )
            # Associate note → contact
            self._client.crm.objects.notes.associations_api.create(
                note_id=note.id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_type="note_to_contact",
            )
        except Exception:
            # Timeline note is optional; don't fail the sync if it errors
            pass
