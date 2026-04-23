"""
HubSpot sync: cerca un contatto per email, lo crea se assente
o aggiorna i campi mancanti se già presente.
Ritorna: (status, contact_id) dove status è "created"|"updated"|"skipped".
"""

import re
from typing import Optional
from hubspot import HubSpot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, ApiException
from hubspot.crm.contacts.models import SimplePublicUpsertObject

CONTACT_SOURCE = "Gmail"
INBOUND_TAG = "Inbound Gmail"

# Proprietà da recuperare dal contatto esistente per capire cosa aggiornare
FETCH_PROPERTIES = [
    "email",
    "firstname",
    "lastname",
    "company",
    "hs_lead_source",
]

# Nomi di dominio email generici che non rappresentano aziende reali
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "protonmail.com", "mail.com",
    "aol.com", "me.com", "msn.com", "libero.it", "virgilio.it",
    "tin.it", "alice.it", "tiscali.it",
}


def _domain_to_company(domain: str) -> Optional[str]:
    """Converte il dominio in nome azienda se non è un provider generico."""
    if not domain or domain.lower() in _GENERIC_DOMAINS:
        return None
    name = domain.split(".")[0]
    return name.capitalize()


def _split_name(full_name: str) -> tuple[str, str]:
    """Divide un nome completo in (firstname, lastname)."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def get_hubspot_client(access_token: str) -> HubSpot:
    return HubSpot(access_token=access_token)


def find_contact_by_email(client: HubSpot, email: str) -> Optional[dict]:
    """
    Cerca il contatto in HubSpot per email.
    Ritorna il dict del contatto (con id + properties) o None.
    """
    try:
        results = client.crm.contacts.search_api.do_search(
            public_object_search_request={
                "filterGroups": [
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
                "properties": FETCH_PROPERTIES,
                "limit": 1,
            }
        )
        if results.total > 0:
            hit = results.results[0]
            return {"id": hit.id, "properties": hit.properties}
    except ApiException:
        pass
    return None


def _build_new_properties(
    sender_name: str,
    sender_email: str,
    domain: str,
) -> dict[str, str]:
    """Costruisce le proprietà per un nuovo contatto."""
    firstname, lastname = _split_name(sender_name) if sender_name else ("", "")
    company = _domain_to_company(domain)

    props: dict[str, str] = {"email": sender_email}
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    props["hs_lead_source"] = CONTACT_SOURCE
    return props


def _build_update_properties(
    existing: dict,
    sender_name: str,
    domain: str,
) -> dict[str, str]:
    """
    Costruisce solo i campi mancanti da aggiornare nel contatto esistente.
    Non sovrascrive campi già valorizzati.
    """
    ep = existing.get("properties", {})
    updates: dict[str, str] = {}

    if sender_name and not ep.get("firstname"):
        firstname, lastname = _split_name(sender_name)
        if firstname:
            updates["firstname"] = firstname
        if lastname and not ep.get("lastname"):
            updates["lastname"] = lastname

    if not ep.get("company"):
        company = _domain_to_company(domain)
        if company:
            updates["company"] = company

    if not ep.get("hs_lead_source"):
        updates["hs_lead_source"] = CONTACT_SOURCE

    return updates


def add_inbound_note(client: HubSpot, contact_id: str, subject: str, sender_email: str) -> None:
    """Aggiunge una nota HubSpot con il dettaglio dell'email ricevuta."""
    try:
        note_body = (
            f"Email inbound ricevuta da: {sender_email}\n"
            f"Oggetto: {subject}\n"
            f"Fonte: {CONTACT_SOURCE} | Tag: {INBOUND_TAG}"
        )
        note = client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create={
                "properties": {
                    "hs_note_body": note_body,
                    "hs_timestamp": _now_ms(),
                },
                "associations": [
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
            }
        )
    except Exception:
        pass  # la nota è opzionale, non blocca il flusso


def _now_ms() -> str:
    import time
    return str(int(time.time() * 1000))


def sync_contact(
    client: HubSpot,
    sender_name: str,
    sender_email: str,
    domain: str,
    subject: str = "",
    add_note: bool = True,
) -> tuple[str, str]:
    """
    Principale: crea o aggiorna il contatto in HubSpot.
    Ritorna (status, contact_id).
    status: "created" | "updated" | "skipped"
    """
    existing = find_contact_by_email(client, sender_email)

    if existing is None:
        # --- CREA ---
        props = _build_new_properties(sender_name, sender_email, domain)
        try:
            created = client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create={
                    "properties": props
                }
            )
            contact_id = created.id
            if add_note:
                add_inbound_note(client, contact_id, subject, sender_email)
            return "created", contact_id
        except ApiException as e:
            # 409 = email duplicata per race condition → riprova con GET
            if e.status == 409:
                existing = find_contact_by_email(client, sender_email)
                if existing:
                    pass  # cade nel ramo update sotto
                else:
                    return "skipped", ""
            else:
                raise

    # --- AGGIORNA ---
    contact_id = existing["id"]
    updates = _build_update_properties(existing, sender_name, domain)

    if updates:
        client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input={"properties": updates},
        )
        if add_note:
            add_inbound_note(client, contact_id, subject, sender_email)
        return "updated", contact_id

    return "skipped", contact_id
