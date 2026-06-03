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

from .models import SenderContact, SyncResult, SyncStatus

CONTACT_SOURCE = "Gmail"
CONTACT_TAG = "Inbound Gmail"


def _build_properties(contact: SenderContact, *, include_source: bool = True) -> dict:
    props: dict[str, str] = {"email": contact.email}

    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    if include_source:
        props["hs_lead_status"] = "NEW"
        # Store the acquisition source in the standard HubSpot field
        props["hs_analytics_source"] = "OTHER_CAMPAIGNS"

    return props


class HubSpotClient:
    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the existing HubSpot contact dict or None."""
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
            result = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if result.total > 0:
                return result.results[0].to_dict()
        except ApiException:
            pass
        return None

    def create_contact(self, contact: SenderContact) -> SyncResult:
        props = _build_properties(contact, include_source=True)
        try:
            result = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return SyncResult(
                status=SyncStatus.CREATED,
                email=contact.email,
                hubspot_id=result.id,
            )
        except ApiException as e:
            # 409 = already exists (race condition)
            if e.status == 409:
                return self._update_or_ignore(contact, None)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                detail=f"Errore API: {e.status}",
            )

    def _update_or_ignore(
        self, contact: SenderContact, existing: Optional[dict]
    ) -> SyncResult:
        if existing is None:
            existing = self.find_contact_by_email(contact.email)
        if existing is None:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                detail="Contatto non trovato dopo conflitto",
            )

        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        # Only patch fields that are currently empty in HubSpot
        updates: dict[str, str] = {}
        if contact.first_name and not existing_props.get("firstname"):
            updates["firstname"] = contact.first_name
        if contact.last_name and not existing_props.get("lastname"):
            updates["lastname"] = contact.last_name
        if contact.company and not existing_props.get("company"):
            updates["company"] = contact.company

        if not updates:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                hubspot_id=contact_id,
                detail="Nessun campo da aggiornare",
            )

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.models.SimplePublicObjectInput(
                    properties=updates
                ),
            )
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=contact.email,
                hubspot_id=contact_id,
                detail=f"Campi aggiornati: {', '.join(updates.keys())}",
            )
        except ApiException as e:
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                hubspot_id=contact_id,
                detail=f"Aggiornamento fallito: {e.status}",
            )

    def upsert_contact(self, contact: SenderContact) -> SyncResult:
        """Create or update a contact, returning a SyncResult."""
        existing = self.find_contact_by_email(contact.email)
        if existing is None:
            return self.create_contact(contact)
        return self._update_or_ignore(contact, existing)
