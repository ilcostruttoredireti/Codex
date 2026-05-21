"""
Gestisce le operazioni su HubSpot CRM:
  - Ricerca contatto per email
  - Creazione nuovo contatto
  - Aggiornamento campi mancanti
  - Creazione attività timeline (nota email ricevuta)
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    SimplePublicObjectInputForCreate,
    ApiException,
)

import config


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    message: str = ""


def _get_client() -> hubspot.Client:
    if not config.HUBSPOT_ACCESS_TOKEN:
        raise ValueError(
            "HUBSPOT_ACCESS_TOKEN non impostato. "
            "Configura la variabile d'ambiente nel file .env"
        )
    return hubspot.Client.create(access_token=config.HUBSPOT_ACCESS_TOKEN)


def _search_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """Cerca un contatto HubSpot per email. Restituisce il contatto o None."""
    from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

    f = Filter(property_name="email", operator="EQ", value=email)
    fg = FilterGroup(filters=[f])
    req = PublicObjectSearchRequest(
        filter_groups=[fg],
        properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
        limit=1,
    )

    try:
        result = client.crm.contacts.search_api.do_search(public_object_search_request=req)
        if result.total > 0:
            return result.results[0]
    except ApiException as e:
        raise RuntimeError(f"Errore ricerca HubSpot: {e}") from e

    return None


def upsert_contact(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
    add_tag: bool = True,
    create_note: bool = True,
    note_subject: str = "",
) -> SyncResult:
    """
    Crea o aggiorna un contatto HubSpot.
    Usa l'email come chiave unica per evitare duplicati.
    """
    if not email or "@" not in email:
        return SyncResult(SyncStatus.IGNORED, email, None, "Email non valida")

    client = _get_client()
    existing = _search_contact_by_email(client, email)

    if existing:
        contact_id = existing.id
        updated = _update_missing_fields(client, existing, first_name, last_name, company)
        status = SyncStatus.UPDATED if updated else SyncStatus.IGNORED
        msg = "Campi aggiornati" if updated else "Nessuna modifica necessaria"
    else:
        contact_id = _create_contact(client, email, first_name, last_name, company)
        status = SyncStatus.CREATED
        msg = "Nuovo contatto creato"

    if create_note and note_subject and status != SyncStatus.IGNORED:
        _create_email_note(client, contact_id, note_subject, email)

    return SyncResult(status=status, email=email, contact_id=contact_id, message=msg)


def _build_properties(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
) -> dict:
    props = {
        "email": email,
        "hs_lead_source": "EMAIL",
        "source_of_last_booking_in_meetings_tool": "Gmail",
    }
    # Usa custom property "contact_source" se disponibile, altrimenti un campo note
    props["website"] = ""  # placeholder — sostituito sotto

    # Rimuoviamo website dal dict e costruiamo solo i campi reali
    del props["website"]

    if first_name:
        props["firstname"] = first_name
    if last_name:
        props["lastname"] = last_name
    if company:
        props["company"] = company

    # Tag "Inbound Gmail" come proprietà di testo libero (richiede custom prop in HS)
    # Usiamo il campo notes_last_contacted o un custom field se configurato.
    # Qui usiamo il campo "lifecyclestage" per tracciare la fonte.
    props["lifecyclestage"] = "lead"

    return props


def _create_contact(
    client: hubspot.Client,
    email: str,
    first_name: str,
    last_name: str,
    company: str,
) -> str:
    """Crea un nuovo contatto e restituisce il suo ID."""
    props = _build_properties(email, first_name, last_name, company)
    body = SimplePublicObjectInputForCreate(properties=props)
    try:
        result = client.crm.contacts.basic_api.create(simple_public_object_input_for_create=body)
        return result.id
    except ApiException as e:
        raise RuntimeError(f"Errore creazione contatto HubSpot: {e}") from e


def _update_missing_fields(
    client: hubspot.Client,
    existing_contact,
    first_name: str,
    last_name: str,
    company: str,
) -> bool:
    """
    Aggiorna solo i campi vuoti nel contatto esistente.
    Restituisce True se almeno un campo è stato aggiornato.
    """
    current = existing_contact.properties or {}
    updates: dict = {}

    if first_name and not current.get("firstname"):
        updates["firstname"] = first_name
    if last_name and not current.get("lastname"):
        updates["lastname"] = last_name
    if company and not current.get("company"):
        updates["company"] = company

    if not updates:
        return False

    from hubspot.crm.contacts import SimplePublicObjectInput

    body = SimplePublicObjectInput(properties=updates)
    try:
        client.crm.contacts.basic_api.update(
            contact_id=existing_contact.id,
            simple_public_object_input=body,
        )
    except ApiException as e:
        raise RuntimeError(f"Errore aggiornamento contatto HubSpot: {e}") from e

    return True


def _create_email_note(
    client: hubspot.Client,
    contact_id: str,
    subject: str,
    sender_email: str,
) -> None:
    """
    Crea una nota di attività associata al contatto per tracciare l'email ricevuta.
    Usa le Engagements API (Notes).
    """
    import time

    note_body = (
        f"Email ricevuta da: {sender_email}\n"
        f"Oggetto: {subject}\n"
        f"Fonte: Gmail (Inbound)\n"
        f"Tag: Inbound Gmail"
    )

    properties = {
        "hs_note_body": note_body,
        "hs_timestamp": str(int(time.time() * 1000)),
    }

    from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput
    from hubspot.crm.associations import AssociationSpec

    try:
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=NoteInput(
                properties=properties,
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
        # La creazione di note è opzionale — non blocca il flusso principale
        pass
