import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

logger = logging.getLogger(__name__)

_GMAIL_SOURCE = "Gmail"
_GMAIL_TAG_NOTE = "Inbound Gmail"


class SyncResult(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"


@dataclass
class SyncOutcome:
    result: SyncResult
    contact_email: str
    contact_id: str | None
    detail: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.result.value}] {self.contact_email}"]
        if self.contact_id:
            parts.append(f"ID={self.contact_id}")
        if self.detail:
            parts.append(f"({self.detail})")
        return " | ".join(parts)


class HubSpotClient:
    def __init__(self, access_token: str):
        self._hs = hubspot.Client.create(access_token=access_token)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> dict | None:
        """Cerca un contatto per email. Restituisce l'oggetto HubSpot o None."""
        search_req = PublicObjectSearchRequest(
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
            properties=["email", "firstname", "lastname", "company", "leadsource"],
            limit=1,
        )
        try:
            result = self._hs.crm.contacts.search_api.do_search(
                public_object_search_request=search_req
            )
            return result.results[0] if result.total > 0 else None
        except ApiException as e:
            logger.error(f"Errore ricerca HubSpot ({email}): {e}")
            return None

    # ------------------------------------------------------------------
    # Create / Update
    # ------------------------------------------------------------------

    def create_contact(self, contact_data) -> SyncOutcome:
        props = _build_create_properties(contact_data)
        try:
            resp = self._hs.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            return SyncOutcome(
                result=SyncResult.CREATED,
                contact_email=contact_data.email,
                contact_id=resp.id,
            )
        except ApiException as e:
            logger.error(f"Errore creazione contatto ({contact_data.email}): {e}")
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                contact_email=contact_data.email,
                contact_id=None,
                detail=f"create error: {e.status}",
            )

    def update_contact(self, contact_id: str, contact_data, existing) -> SyncOutcome:
        props = _build_update_properties(contact_data, existing.properties)
        if not props:
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                contact_email=contact_data.email,
                contact_id=contact_id,
                detail="nessun campo nuovo da aggiornare",
            )
        try:
            self._hs.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(properties=props),
            )
            return SyncOutcome(
                result=SyncResult.UPDATED,
                contact_email=contact_data.email,
                contact_id=contact_id,
                detail=f"aggiornati: {', '.join(props.keys())}",
            )
        except ApiException as e:
            logger.error(f"Errore aggiornamento contatto ({contact_data.email}): {e}")
            return SyncOutcome(
                result=SyncResult.SKIPPED,
                contact_email=contact_data.email,
                contact_id=contact_id,
                detail=f"update error: {e.status}",
            )

    # ------------------------------------------------------------------
    # Activity note (opzionale)
    # ------------------------------------------------------------------

    def create_activity_note(
        self, contact_id: str, subject: str, email_date: str
    ) -> None:
        """Crea una nota attività 'Email ricevuta' associata al contatto."""
        timestamp_ms = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        body = (
            f"📧 Email ricevuta via Gmail\n"
            f"Tag: {_GMAIL_TAG_NOTE}\n"
            f"Oggetto: {subject}\n"
            f"Data: {email_date}"
        )
        try:
            note = self._hs.crm.objects.notes.basic_api.create(
                simple_public_object_input_for_create=hubspot.crm.objects.notes.SimplePublicObjectInputForCreate(
                    properties={
                        "hs_note_body": body,
                        "hs_timestamp": timestamp_ms,
                    }
                )
            )
            # Associa nota al contatto
            self._hs.crm.associations.v4.basic_api.create(
                object_type="notes",
                object_id=note.id,
                to_object_type="contacts",
                to_object_id=contact_id,
                association_spec=[
                    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
                ],
            )
            logger.debug(f"Nota attività creata per contatto {contact_id}")
        except Exception as e:
            # Non bloccante: logga e continua
            logger.warning(f"Impossibile creare nota attività per {contact_id}: {e}")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _build_create_properties(cd) -> dict[str, str]:
    props: dict[str, str] = {"email": cd.email, "leadsource": _GMAIL_SOURCE}
    if cd.first_name:
        props["firstname"] = cd.first_name
    if cd.last_name:
        props["lastname"] = cd.last_name
    if cd.company:
        props["company"] = cd.company
    return props


def _build_update_properties(cd, existing_props: dict) -> dict[str, str]:
    """Ritorna solo i campi vuoti/mancanti nel contatto esistente."""
    candidates: dict[str, str] = {}
    if cd.first_name and not existing_props.get("firstname"):
        candidates["firstname"] = cd.first_name
    if cd.last_name and not existing_props.get("lastname"):
        candidates["lastname"] = cd.last_name
    if cd.company and not existing_props.get("company"):
        candidates["company"] = cd.company
    if not existing_props.get("leadsource"):
        candidates["leadsource"] = _GMAIL_SOURCE
    return candidates
