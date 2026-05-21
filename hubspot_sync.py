"""
Sincronizzazione contatti con HubSpot.
Crea nuovi contatti o aggiorna quelli esistenti senza creare duplicati.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
    ApiException,
)
from hubspot.crm.contacts.models import Filter, FilterGroup, PublicObjectSearchRequest

from gmail_reader import SenderInfo


class SyncStatus(Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    reason: str = ""


CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"


def _build_properties(info: SenderInfo) -> dict:
    props = {
        "email": info.email,
        "hs_lead_status": "NEW",
        "leadsource": CONTACT_SOURCE,
    }
    if info.firstname:
        props["firstname"] = info.firstname
    if info.lastname:
        props["lastname"] = info.lastname
    if info.company:
        props["company"] = info.company
    return props


def _search_contact(client: hubspot.HubSpot, email: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Ritorna il record o None."""
    search_request = PublicObjectSearchRequest(
        filter_groups=[
            FilterGroup(filters=[
                Filter(property_name="email", operator="EQ", value=email)
            ])
        ],
        properties=["email", "firstname", "lastname", "company", "leadsource"],
        limit=1,
    )
    response = client.crm.contacts.search_api.do_search(
        public_object_search_request=search_request
    )
    results = response.results
    return results[0].to_dict() if results else None


def _merge_properties(existing: dict, info: SenderInfo) -> dict:
    """Costruisce le proprietà da aggiornare (solo campi vuoti nell'esistente)."""
    existing_props = existing.get("properties", {})
    updates = {}

    if not existing_props.get("firstname") and info.firstname:
        updates["firstname"] = info.firstname
    if not existing_props.get("lastname") and info.lastname:
        updates["lastname"] = info.lastname
    if not existing_props.get("company") and info.company:
        updates["company"] = info.company
    # Imposta sempre la sorgente se mancante
    if not existing_props.get("leadsource"):
        updates["leadsource"] = CONTACT_SOURCE

    return updates


def sync_contact(client: hubspot.HubSpot, info: SenderInfo) -> SyncResult:
    """Crea o aggiorna un contatto HubSpot a partire da un SenderInfo Gmail."""
    try:
        existing = _search_contact(client, info.email)

        if existing is None:
            # Crea nuovo contatto
            props = _build_properties(info)
            new_contact = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            contact_id = new_contact.id
            return SyncResult(
                status=SyncStatus.CREATED,
                email=info.email,
                contact_id=contact_id,
            )

        else:
            contact_id = existing["id"]
            updates = _merge_properties(existing, info)

            if updates:
                client.crm.contacts.basic_api.update(
                    contact_id=contact_id,
                    simple_public_object_input=SimplePublicObjectInput(
                        properties=updates
                    ),
                )
                return SyncResult(
                    status=SyncStatus.UPDATED,
                    email=info.email,
                    contact_id=contact_id,
                    reason=f"Campi aggiornati: {', '.join(updates.keys())}",
                )
            else:
                return SyncResult(
                    status=SyncStatus.IGNORED,
                    email=info.email,
                    contact_id=contact_id,
                    reason="Nessun campo nuovo da aggiornare",
                )

    except ApiException as e:
        return SyncResult(
            status=SyncStatus.IGNORED,
            email=info.email,
            contact_id=None,
            reason=f"Errore HubSpot API: {e.status} {e.reason}",
        )
