"""HubSpot client: creates / updates contacts and logs timeline activities."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

from utils import domain_to_company

INBOUND_TAG = "Inbound Gmail"
CONTACT_SOURCE = "Gmail"


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str


class HubSpotClient:
    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_contact(
        self,
        email: str,
        first_name: str = "",
        last_name: str = "",
        domain: str = "",
        subject: str = "",
        date: str = "",
    ) -> SyncResult:
        """Upsert a contact derived from an inbound Gmail message."""
        if not email or "@" not in email:
            return SyncResult(SyncStatus.IGNORED, email, "")

        existing = self._find_contact(email)
        if existing:
            contact_id = existing["id"]
            updated = self._update_contact(existing, first_name, last_name, domain)
            self._log_email_activity(contact_id, email, subject, date)
            status = SyncStatus.UPDATED if updated else SyncStatus.IGNORED
            return SyncResult(status, email, contact_id)

        contact_id = self._create_contact(email, first_name, last_name, domain)
        self._log_email_activity(contact_id, email, subject, date)
        return SyncResult(SyncStatus.CREATED, email, contact_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_contact(self, email: str) -> Optional[dict]:
        search_request = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email,
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
                r = response.results[0]
                return {"id": r.id, "properties": r.properties}
        except ApiException:
            pass
        return None

    def _create_contact(
        self, email: str, first_name: str, last_name: str, domain: str
    ) -> str:
        company = domain_to_company(domain)
        properties = {
            "email": email,
            "hs_lead_status": "NEW",
            "leadsource": CONTACT_SOURCE,
        }
        if first_name:
            properties["firstname"] = first_name
        if last_name:
            properties["lastname"] = last_name
        if company:
            properties["company"] = company

        obj = SimplePublicObjectInputForCreate(
            properties=properties,
            associations=[],
        )
        result = self._client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=obj
        )
        return result.id

    def _update_contact(
        self, existing: dict, first_name: str, last_name: str, domain: str
    ) -> bool:
        """Fill in missing fields only. Returns True if any field was updated."""
        props = existing.get("properties", {})
        updates: dict[str, str] = {}

        if first_name and not props.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not props.get("lastname"):
            updates["lastname"] = last_name

        company = domain_to_company(domain)
        if company and not props.get("company"):
            updates["company"] = company

        if not updates:
            return False

        self._client.crm.contacts.basic_api.update(
            contact_id=existing["id"],
            simple_public_object_input=SimplePublicObjectInput(properties=updates),
        )
        return True

    def _log_email_activity(
        self, contact_id: str, email: str, subject: str, date: str
    ) -> None:
        """Create a note on the contact timeline recording the inbound email."""
        body = f"📧 Email inbound ricevuta da {email}"
        if subject:
            body += f"\nOggetto: {subject}"
        if date:
            body += f"\nData: {date}"
        body += f"\nTag: {INBOUND_TAG}"

        try:
            self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": str(int(time.time() * 1000)),
                    },
                    associations=[
                        {
                            "to": {"id": contact_id},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": 202,  # Note → Contact
                                }
                            ],
                        }
                    ],
                )
            )
        except Exception:
            # Timeline activity is optional — never fail the sync because of it
            pass
