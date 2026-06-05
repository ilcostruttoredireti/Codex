"""HubSpot CRM client — create/update contacts with deduplication by email."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    ApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: str


class HubSpotClient:
    _LEAD_SOURCE = "Gmail"
    _TAG_PROPERTY = "hs_lead_status"  # reuse for tagging note in description
    _INBOUND_TAG = "Inbound Gmail"

    def __init__(self, access_token: str) -> None:
        self._client = hubspot.Client.create(access_token=access_token)

    def upsert_contact(
        self,
        email: str,
        first_name: str = "",
        last_name: str = "",
        company: str = "",
    ) -> SyncResult:
        existing = self._find_by_email(email)

        if existing is None:
            contact_id = self._create_contact(email, first_name, last_name, company)
            return SyncResult(SyncStatus.CREATED, email, contact_id)

        contact_id = existing["id"]
        props = existing.get("properties", {})
        updates: dict[str, str] = {}

        if first_name and not props.get("firstname"):
            updates["firstname"] = first_name
        if last_name and not props.get("lastname"):
            updates["lastname"] = last_name
        if company and not props.get("company"):
            updates["company"] = company

        if updates:
            self._update_contact(contact_id, updates)
            return SyncResult(SyncStatus.UPDATED, email, contact_id)

        return SyncResult(SyncStatus.IGNORED, email, contact_id)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _find_by_email(self, email: str) -> Optional[dict]:
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
            properties=["email", "firstname", "lastname", "company"],
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
        except ApiException as exc:
            raise RuntimeError(f"HubSpot search failed: {exc}") from exc

        results = resp.results
        if results:
            return {"id": results[0].id, "properties": results[0].properties}
        return None

    def _create_contact(
        self, email: str, first_name: str, last_name: str, company: str
    ) -> str:
        properties: dict[str, str] = {
            "email": email,
            "hs_lead_source": self._LEAD_SOURCE,
        }
        if first_name:
            properties["firstname"] = first_name
        if last_name:
            properties["lastname"] = last_name
        if company:
            properties["company"] = company

        payload = SimplePublicObjectInputForCreate(properties=properties)
        try:
            resp = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=payload
            )
        except ApiException as exc:
            # 409 Conflict = duplicate; extract existing id from error body
            if exc.status == 409:
                return self._extract_conflict_id(str(exc.body))
            raise RuntimeError(f"HubSpot create failed: {exc}") from exc

        self._add_activity_note(resp.id, email)
        return resp.id

    def _update_contact(self, contact_id: str, properties: dict[str, str]) -> None:
        from hubspot.crm.contacts import SimplePublicObjectInput

        payload = SimplePublicObjectInput(properties=properties)
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=payload,
            )
        except ApiException as exc:
            raise RuntimeError(f"HubSpot update failed: {exc}") from exc

    def _add_activity_note(self, contact_id: str, email: str) -> None:
        """Create an engagement note recording the inbound Gmail contact."""
        try:
            from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteCreate
            from hubspot.crm.associations import AssociationSpec

            note_body = {
                "hs_note_body": f"Contatto acquisito da email inbound Gmail: {email}",
                "hs_timestamp": _now_ms(),
            }
            note_payload = NoteCreate(properties=note_body)
            note_resp = self._client.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=note_payload
            )
            # Associate note → contact
            self._client.crm.associations.v4.basic_api.create(
                object_type="notes",
                object_id=note_resp.id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_spec=[
                    AssociationSpec(
                        association_category="HUBSPOT_DEFINED",
                        association_type_id=202,
                    )
                ],
            )
        except Exception:
            # Timeline note is optional — don't fail the whole sync
            pass

    @staticmethod
    def _extract_conflict_id(body: str) -> str:
        import json, re

        try:
            data = json.loads(body)
            return str(data["message"].split("vid=")[1].split(";")[0])
        except Exception:
            match = re.search(r"vid=(\d+)", body)
            return match.group(1) if match else "unknown"


def _now_ms() -> int:
    import time
    return int(time.time() * 1000)
