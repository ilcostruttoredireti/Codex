"""HubSpot CRM client: cerca, crea e aggiorna contatti."""

import logging
from typing import Optional

import hubspot
from hubspot.crm.contacts import SimplePublicObjectInputForCreate, SimplePublicObjectInput
from hubspot.crm.contacts.exceptions import ApiException
from hubspot.crm.timeline import TimelineEvent, TimelineEventIFrame

logger = logging.getLogger(__name__)

# ID evento custom per timeline (opzionale — deve essere creato in HubSpot)
TIMELINE_EVENT_TYPE_ID = None  # imposta se hai un EventType personalizzato


def get_hubspot_client(access_token: str) -> hubspot.Client:
    return hubspot.Client.create(access_token=access_token)


def find_contact_by_email(client: hubspot.Client, email: str) -> Optional[dict]:
    """
    Cerca un contatto per email. Restituisce il dizionario del contatto o None.
    """
    try:
        from hubspot.crm.contacts import PublicObjectSearchRequest, Filter, FilterGroup

        filter_obj = Filter(property_name="email", operator="EQ", value=email)
        filter_group = FilterGroup(filters=[filter_obj])
        search_req = PublicObjectSearchRequest(
            filter_groups=[filter_group],
            properties=["firstname", "lastname", "email", "company", "hs_lead_source"],
            limit=1,
        )
        response = client.crm.contacts.search_api.do_search(
            public_object_search_request=search_req
        )
        results = response.results
        if results:
            contact = results[0]
            return {
                "id": contact.id,
                "properties": contact.properties,
            }
        return None
    except ApiException as e:
        logger.error("Errore ricerca contatto HubSpot (%s): %s", email, e)
        raise


def create_contact(client: hubspot.Client, sender: dict) -> dict:
    """
    Crea un nuovo contatto HubSpot dai dati del mittente.
    Restituisce il contatto creato.
    """
    props = _build_properties(sender, is_new=True)

    try:
        contact_input = SimplePublicObjectInputForCreate(properties=props)
        response = client.crm.contacts.basic_api.create(
            simple_public_object_input_for_create=contact_input
        )
        logger.info("Contatto CREATO: %s (ID: %s)", sender["email"], response.id)
        return {"id": response.id, "properties": response.properties}
    except ApiException as e:
        # 409 = contatto già esistente (race condition)
        if e.status == 409:
            logger.warning("Conflitto creazione (409) per %s, cerco il contatto.", sender["email"])
            return find_contact_by_email(client, sender["email"])
        logger.error("Errore creazione contatto (%s): %s", sender["email"], e)
        raise


def update_contact(client: hubspot.Client, contact_id: str, sender: dict, existing_props: dict) -> dict:
    """
    Aggiorna un contatto esistente con i campi mancanti/vuoti.
    Non sovrascrive valori già presenti.
    """
    new_props = _build_properties(sender, is_new=False)

    # Aggiorna solo i campi vuoti nel contatto esistente
    to_update = {}
    for key, value in new_props.items():
        existing_val = existing_props.get(key)
        if not existing_val and value:
            to_update[key] = value

    if not to_update:
        logger.info("Contatto %s (%s): nessun aggiornamento necessario.", sender["email"], contact_id)
        return {"id": contact_id, "properties": existing_props}

    try:
        contact_input = SimplePublicObjectInput(properties=to_update)
        response = client.crm.contacts.basic_api.update(
            contact_id=contact_id,
            simple_public_object_input=contact_input,
        )
        logger.info(
            "Contatto AGGIORNATO: %s (ID: %s) — campi: %s",
            sender["email"], contact_id, list(to_update.keys())
        )
        return {"id": response.id, "properties": response.properties}
    except ApiException as e:
        logger.error("Errore aggiornamento contatto %s: %s", contact_id, e)
        raise


def _build_properties(sender: dict, is_new: bool) -> dict:
    """Costruisce le proprietà HubSpot da un dizionario mittente."""
    props = {
        "email": sender["email"],
        "hs_lead_source": "Gmail",
    }

    if sender.get("first_name"):
        props["firstname"] = sender["first_name"]
    if sender.get("last_name"):
        props["lastname"] = sender["last_name"]
    if sender.get("company"):
        props["company"] = sender["company"]

    return props


def add_timeline_note(client: hubspot.Client, contact_id: str, subject: str, date: str, email: str):
    """
    Aggiunge una nota/engagement 'email ricevuta' alla timeline del contatto.
    Usa l'API Engagements (note) come traccia dell'attività.
    """
    try:
        from hubspot.crm.objects.notes import SimplePublicObjectInputForCreate as NoteInput

        note_body = f"Email ricevuta da {email}\nOggetto: {subject}\nData: {date}"
        props = {
            "hs_note_body": note_body,
            "hs_timestamp": _parse_date_to_ms(date),
        }
        note_input = NoteInput(
            properties=props,
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
        client.crm.objects.notes.basic_api.create(
            simple_public_object_input_for_create=note_input
        )
        logger.info("Nota timeline aggiunta per contatto %s", contact_id)
    except Exception as e:
        # Non bloccare il flusso principale se la nota fallisce
        logger.warning("Impossibile aggiungere nota timeline per %s: %s", contact_id, e)


def _parse_date_to_ms(date_str: str) -> str:
    """Converte stringa data RFC2822 in millisecondi epoch per HubSpot."""
    import email.utils
    import time

    try:
        parsed = email.utils.parsedate(date_str)
        if parsed:
            return str(int(time.mktime(parsed) * 1000))
    except Exception:
        pass
    return str(int(__import__("time").time() * 1000))
