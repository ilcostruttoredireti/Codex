import logging
import time
from typing import Optional

import requests
from hubspot import HubSpot
from hubspot.crm.contacts import ApiException
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

logger = logging.getLogger(__name__)

STATUS_CREATED = "Creato"
STATUS_UPDATED = "Aggiornato"
STATUS_IGNORED = "Ignorato"

# HubSpot CRM API base URL used for direct REST calls (e.g. notes)
_HS_API_BASE = "https://api.hubapi.com"
# Association type: Note → Contact (HUBSPOT_DEFINED id 202)
_NOTE_TO_CONTACT_TYPE_ID = 202


class HubSpotSync:
    def __init__(self, access_token: str):
        self._token = access_token
        self._client = HubSpot(access_token=access_token)
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def sync_contact(self, contact: dict) -> tuple[str, str]:
        """Upsert *contact* in HubSpot. Returns (status_label, contact_id)."""
        email = contact["email"]
        existing = self._find_by_email(email)

        if existing:
            contact_id = existing["id"]
            updated = self._fill_missing_fields(contact_id, existing["properties"], contact)
            status = STATUS_UPDATED if updated else STATUS_IGNORED
        else:
            contact_id = self._create_contact(contact)
            self._add_inbound_note(contact_id, contact)
            status = STATUS_CREATED

        return status, contact_id

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _find_by_email(self, email: str) -> Optional[dict]:
        flt = Filter(property_name="email", operator="EQ", value=email)
        grp = FilterGroup(filters=[flt])
        req = PublicObjectSearchRequest(
            filter_groups=[grp],
            properties=["email", "firstname", "lastname", "company", "leadsource"],
            limit=1,
        )
        try:
            result = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=req
            )
            if result.total > 0:
                r = result.results[0]
                return {"id": r.id, "properties": r.properties}
        except ApiException as exc:
            logger.error("HubSpot search failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def _create_contact(self, contact: dict) -> str:
        props: dict[str, str] = {"email": contact["email"], "leadsource": "Gmail"}
        for field in ("firstname", "lastname", "company"):
            if contact.get(field):
                props[field] = contact[field]

        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return result.id
        except ApiException as exc:
            logger.error("HubSpot create failed: %s", exc)
            raise

    # ------------------------------------------------------------------
    # Update (fill blanks only — never overwrite existing data)
    # ------------------------------------------------------------------

    def _fill_missing_fields(
        self, contact_id: str, existing: dict, contact: dict
    ) -> bool:
        updates: dict[str, str] = {}

        for field in ("firstname", "lastname", "company"):
            if not existing.get(field) and contact.get(field):
                updates[field] = contact[field]

        if not existing.get("leadsource"):
            updates["leadsource"] = "Gmail"

        if not updates:
            return False

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            return True
        except ApiException as exc:
            logger.error("HubSpot update failed for %s: %s", contact_id, exc)
            return False

    # ------------------------------------------------------------------
    # Timeline note  (optional — failures are non-fatal)
    # ------------------------------------------------------------------

    def _add_inbound_note(self, contact_id: str, contact: dict) -> None:
        note_body = (
            "Tag: Inbound Gmail\n"
            f"Subject: {contact.get('subject') or 'N/A'}\n"
            f"Date: {contact.get('date') or 'N/A'}"
        )
        payload = {
            "properties": {
                "hs_note_body": note_body,
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
                        }
                    ],
                }
            ],
        }
        try:
            resp = requests.post(
                f"{_HS_API_BASE}/crm/v3/objects/notes",
                headers=self._headers,
                json=payload,
                timeout=10,
            )
            resp.raise_for_status()
            logger.debug("Note added for contact %s", contact_id)
        except Exception as exc:
            logger.warning("Could not add note for contact %s: %s", contact_id, exc)
