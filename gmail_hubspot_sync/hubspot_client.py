import logging
import time
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import PublicObjectSearchRequest

logger = logging.getLogger(__name__)

# HubSpot rate-limit: ~100 req/10s for search, 100 req/10s for CRM write
_RETRY_WAIT = 2  # seconds between retries
_MAX_RETRIES = 3


class HubSpotClient:
    def __init__(self, api_token: str):
        self._client = hubspot.Client.create(access_token=api_token)

    # ------------------------------------------------------------------
    # Contact lookup
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing contact dict or None."""
        request = PublicObjectSearchRequest(
            filter_groups=[
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email,
                        }
                    ]
                }
            ],
            properties=["email", "firstname", "lastname", "company", "hs_lead_source"],
            limit=1,
        )

        for attempt in range(_MAX_RETRIES):
            try:
                response = self._client.crm.contacts.search_api.do_search(
                    public_object_search_request=request
                )
                results = response.results
                if results:
                    contact = results[0]
                    return {"id": contact.id, "properties": contact.properties}
                return None
            except ApiException as exc:
                if exc.status == 429 and attempt < _MAX_RETRIES - 1:
                    logger.warning("HubSpot rate limit hit, waiting %ds…", _RETRY_WAIT)
                    time.sleep(_RETRY_WAIT * (attempt + 1))
                else:
                    raise

        return None

    # ------------------------------------------------------------------
    # Contact create / update
    # ------------------------------------------------------------------

    def create_contact(self, properties: dict) -> str:
        """Create a new contact and return its HubSpot ID."""
        payload = SimplePublicObjectInputForCreate(properties=properties)

        for attempt in range(_MAX_RETRIES):
            try:
                result = self._client.crm.contacts.basic_api.create(
                    simple_public_object_input_for_create=payload
                )
                return result.id
            except ApiException as exc:
                if exc.status == 409:
                    # Conflict: contact exists — search and return ID
                    logger.debug("Contact already exists (409), falling back to search.")
                    existing = self.find_contact_by_email(properties["email"])
                    if existing:
                        return existing["id"]
                    raise
                if exc.status == 429 and attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_WAIT * (attempt + 1))
                else:
                    raise

        raise RuntimeError("Failed to create contact after retries.")

    def update_contact(self, contact_id: str, properties: dict) -> None:
        """Patch only the provided properties on an existing contact."""
        payload = SimplePublicObjectInput(properties=properties)

        for attempt in range(_MAX_RETRIES):
            try:
                self._client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=payload,
                )
                return
            except ApiException as exc:
                if exc.status == 429 and attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_WAIT * (attempt + 1))
                else:
                    raise

    # ------------------------------------------------------------------
    # Activity timeline (note)
    # ------------------------------------------------------------------

    def add_email_received_note(
        self,
        contact_id: str,
        sender_email: str,
        subject: str,
        received_date: str,
    ) -> None:
        """Attach a note to the contact recording the inbound email."""
        note_body = (
            f"Inbound Gmail received\n"
            f"From: {sender_email}\n"
            f"Subject: {subject or '(no subject)'}\n"
            f"Date: {received_date}\n"
            f"Tag: Inbound Gmail"
        )

        properties = {
            "hs_note_body": note_body,
            "hs_timestamp": str(int(time.time() * 1000)),
        }

        associations = [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,  # note → contact
                    }
                ],
            }
        ]

        for attempt in range(_MAX_RETRIES):
            try:
                self._client.crm.objects.notes.basic_api.create(
                    simple_public_object_input_for_create={
                        "properties": properties,
                        "associations": associations,
                    }
                )
                return
            except ApiException as exc:
                if exc.status == 429 and attempt < _MAX_RETRIES - 1:
                    time.sleep(_RETRY_WAIT * (attempt + 1))
                elif exc.status in (400, 404):
                    # Notes API may require different association format; log and skip
                    logger.warning("Could not create note for contact %s: %s", contact_id, exc)
                    return
                else:
                    raise
