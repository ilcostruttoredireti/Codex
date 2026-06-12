import logging
import time
from typing import Any, Dict, Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)

logger = logging.getLogger(__name__)


class HubSpotClient:
    def __init__(self, access_token: str):
        self._client = hubspot.Client.create(access_token=access_token)

    # ── Ricerca ───────────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Cerca un contatto HubSpot per email (corrispondenza esatta).
        Restituisce {id, properties} oppure None se non trovato.
        """
        search_req = PublicObjectSearchRequest(
            filter_groups=[
                FilterGroup(
                    filters=[
                        Filter(
                            property_name="email",
                            operator="EQ",
                            value=email.lower(),
                        )
                    ]
                )
            ],
            properties=["email", "firstname", "lastname", "company", "lead_source"],
            limit=1,
        )
        try:
            resp = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            if resp.results:
                c = resp.results[0]
                return {"id": c.id, "properties": c.properties}
            return None
        except ApiException as exc:
            logger.error(f"HubSpot search error per {email}: {exc}")
            return None

    # ── Creazione ─────────────────────────────────────────────────────────────

    def create_contact(
        self,
        email: str,
        first_name: Optional[str],
        last_name: Optional[str],
        company: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        """
        Crea un nuovo contatto HubSpot.
        Imposta lead_source='Gmail' e crea una nota 'Inbound Gmail'.
        """
        props: Dict[str, str] = {"email": email, "lead_source": "Gmail"}
        if first_name:
            props["firstname"] = first_name
        if last_name:
            props["lastname"] = last_name
        if company:
            props["company"] = company

        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            self._add_note(contact.id, "Contatto acquisito via Gmail (Inbound Gmail)")
            return {"id": contact.id, "properties": contact.properties}
        except ApiException as exc:
            logger.error(f"HubSpot create error per {email}: {exc}")
            return None

    # ── Aggiornamento ─────────────────────────────────────────────────────────

    def update_contact(self, contact_id: str, updates: Dict[str, str]) -> bool:
        """
        Aggiorna i campi di un contatto esistente.
        Restituisce True in caso di successo.
        """
        if not updates:
            return True
        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=updates),
            )
            return True
        except ApiException as exc:
            logger.error(f"HubSpot update error per contatto {contact_id}: {exc}")
            return False

    # ── Note / Attività ───────────────────────────────────────────────────────

    def _add_note(self, contact_id: str, body: str):
        """
        Crea una nota associata al contatto.
        Operazione opzionale — gli errori vengono solo loggati.
        """
        try:
            from hubspot.crm.objects import SimplePublicObjectInputForCreate as ObjCreate

            note = self._client.crm.objects.basic_api.create(
                object_type="notes",
                simple_public_object_input_for_create=ObjCreate(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": str(int(time.time() * 1000)),
                    }
                ),
            )
            # Associa nota → contatto (tipo 202 = note_to_contact, HUBSPOT_DEFINED)
            self._client.crm.associations.v4.basic_api.create(
                object_type="notes",
                object_id=note.id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_spec=[
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 202,
                    }
                ],
            )
        except Exception as exc:
            # La nota è opzionale: non blocca il flusso principale
            logger.debug(f"Nota non aggiunta al contatto {contact_id}: {exc}")
