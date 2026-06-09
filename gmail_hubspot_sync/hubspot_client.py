import logging
from typing import Optional

from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicUpsertObject

from .models import ContactInfo

logger = logging.getLogger(__name__)

INBOUND_GMAIL_TAG = "Inbound Gmail"


class HubSpotClient:
    def __init__(self, access_token: str, contact_source: str = "Gmail"):
        self._client = HubSpot(access_token=access_token)
        self._contact_source = contact_source

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact dict or None."""
        try:
            result = self._client.crm.contacts.basic_api.get_by_id(
                contact_id=email,
                id_property="email",
                properties=["email", "firstname", "lastname", "company", "hs_analytics_source"],
                archived=False,
            )
            return {"id": result.id, "properties": result.properties}
        except ApiException as exc:
            if exc.status == 404:
                return None
            logger.error("HubSpot lookup error for %s: %s", email, exc)
            raise

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create_contact(self, contact: ContactInfo) -> str:
        """Create a new HubSpot contact and return its ID."""
        props = contact.to_hubspot_properties(self._contact_source)
        body = SimplePublicObjectInputForCreate(
            properties=props,
            associations=[],
        )
        result = self._client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=body
        )
        logger.info("Created HubSpot contact id=%s for %s", result.id, contact.email)
        self._add_inbound_note(result.id, contact.email)
        return result.id

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update_contact(self, hubspot_id: str, contact: ContactInfo, existing_props: dict) -> None:
        """Update only fields that are missing/blank in the existing record."""
        updates: dict = {}

        def _missing(key: str) -> bool:
            val = existing_props.get(key)
            return not val or str(val).strip() == ""

        if _missing("firstname") and contact.first_name:
            updates["firstname"] = contact.first_name
        if _missing("lastname") and contact.last_name:
            updates["lastname"] = contact.last_name
        if _missing("company") and contact.company:
            updates["company"] = contact.company

        if updates:
            from hubspot.crm.contacts import SimplePublicObjectInput
            self._client.crm.contacts.basic_api.update(
                contact_id=hubspot_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            logger.info("Updated HubSpot contact id=%s fields=%s", hubspot_id, list(updates))

    # ------------------------------------------------------------------
    # Timeline activity
    # ------------------------------------------------------------------

    def _add_inbound_note(self, contact_id: str, email: str) -> None:
        """Log an engagement note for the inbound email event."""
        try:
            from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput

            note_body = NoteInput(
                properties={
                    "hs_note_body": f"Contatto acquisito da email in arrivo su Gmail ({email})",
                    "hs_timestamp": _now_ms(),
                    "hs_note_status": "LOGGED",
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
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=note_body
            )
        except Exception as exc:
            # Notes are optional; don't fail the whole sync if they break
            logger.debug("Could not create note for contact %s: %s", contact_id, exc)


def _now_ms() -> str:
    import time
    return str(int(time.time() * 1000))
