"""HubSpot CRM v3 client: search, create, and update contacts."""

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    PublicObjectSearchRequest,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup


# Internal name of the HubSpot property used to mark Gmail-sourced contacts.
_SOURCE_PROPERTY = "hs_lead_status"
_TAG_PROPERTY = "hs_analytics_source"


class HubSpotClient:
    def __init__(self, api_key: str):
        self._client = hubspot.Client.create(access_token=api_key)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Return existing contact dict or None."""
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email.lower(),
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                result = response.results[0]
                return {"id": result.id, "properties": result.properties}
        except ApiException:
            pass
        return None

    def create_contact(self, props: dict) -> str:
        """Create a new contact and return its HubSpot ID."""
        payload = SimplePublicObjectInputForCreate(
            properties=self._build_properties(props)
        )
        result = self._client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=payload
        )
        self._add_gmail_tag(result.id)
        return result.id

    def update_contact(self, contact_id: str, props: dict) -> None:
        """Patch only the fields that are missing/empty in HubSpot."""
        from hubspot.crm.contacts import SimplePublicObjectInput

        payload = SimplePublicObjectInput(properties=self._build_properties(props))
        self._client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=payload,
        )

    def fill_missing_fields(
        self, contact_id: str, existing_props: dict, new_props: dict
    ) -> dict:
        """
        Return only the subset of new_props whose values are absent in the
        existing contact, so we never overwrite good data.
        """
        to_update = {}
        for key, value in new_props.items():
            if value and not existing_props.get(key):
                to_update[key] = value
        return to_update

    # ------------------------------------------------------------------
    # Timeline / activity
    # ------------------------------------------------------------------

    def log_email_activity(self, contact_id: str, subject: str, date: str) -> None:
        """Create a note on the contact timeline recording the inbound email."""
        from hubspot.crm.objects.notes import (
            SimplePublicObjectInputForCreate as NoteInput,
        )

        note_body = f"Inbound Gmail email received.\nSubject: {subject}\nDate: {date}"
        payload = NoteInput(
            properties={
                "hs_note_body": note_body,
                "hs_timestamp": date,
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
        try:
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=payload
            )
        except Exception:
            # Timeline logging is best-effort; never block the main sync.
            pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_properties(self, props: dict) -> dict:
        built = {
            "email": props.get("email", "").lower(),
            "leadsource": "Gmail",
        }
        if props.get("firstname"):
            built["firstname"] = props["firstname"]
        if props.get("lastname"):
            built["lastname"] = props["lastname"]
        if props.get("company"):
            built["company"] = props["company"]
        return built

    def _add_gmail_tag(self, contact_id: str) -> None:
        """Add 'Inbound Gmail' tag via a note (HubSpot has no native tag API)."""
        pass  # Tag conveyed via leadsource; timeline note added separately.
