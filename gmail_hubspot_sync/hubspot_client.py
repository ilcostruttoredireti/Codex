"""
hubspot_client.py — Wrapper per l'API HubSpot Contacts v3.

Funzionalità:
- Ricerca contatto per email (chiave univoca)
- Creazione nuovo contatto con tutti i campi richiesti
- Aggiornamento contatto esistente (solo campi vuoti/mancanti)
- Aggiunta nota/attività alla timeline del contatto
- Aggiunta tag 'Inbound Gmail' tramite proprietà hs_lead_status o note
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
    SimplePublicObjectInput,
    SimplePublicObjectInputForCreate,
)
from hubspot.crm.timeline import ApiException as TimelineApiException

from config import config
from email_parser import SenderInfo, company_from_domain
from logger import get_logger

log = get_logger("hubspot", config.log_file)


class SyncStatus(Enum):
    CREATED = auto()
    UPDATED = auto()
    IGNORED = auto()


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    contact_id: Optional[str]
    detail: str = ""

    def __str__(self) -> str:
        icon = {"CREATED": "🟢", "UPDATED": "🔵", "IGNORED": "⚪"}.get(
            self.status.name, "?"
        )
        id_part = f"  id={self.contact_id}" if self.contact_id else ""
        detail_part = f"  ({self.detail})" if self.detail else ""
        return f"{icon} [{self.status.name}]  {self.contact_email}{id_part}{detail_part}"


# Alias locale per leggibilità interna
_company_from_domain = company_from_domain


# ── Client ─────────────────────────────────────────────────────────────────

class HubSpotClient:
    def __init__(self) -> None:
        self._client = hubspot.Client.create(
            access_token=config.hubspot_access_token
        )

    # ── Ricerca ────────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """
        Cerca il contatto per email.
        Ritorna il dict del contatto HubSpot o None se non trovato.
        """
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
                return response.results[0].to_dict()
        except ApiException as exc:
            log.error(f"Errore nella ricerca HubSpot per {email}: {exc}")
        return None

    # ── Creazione ──────────────────────────────────────────────────────────

    def create_contact(self, info: SenderInfo) -> SyncResult:
        """Crea un nuovo contatto in HubSpot con tutti i campi disponibili."""
        company = _company_from_domain(info.company_domain)
        properties = {
            "email": info.email,
            "hs_lead_status": config.contact_tag,
        }
        if info.first_name:
            properties["firstname"] = info.first_name
        if info.last_name:
            properties["lastname"] = info.last_name
        if company:
            properties["company"] = company

        # Fonte contatto — campo nativo HubSpot
        # 'leadsource' è la proprietà standard per la fonte
        properties["leadsource"] = config.contact_source

        try:
            contact = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=properties
                )
            )
            contact_id = contact.id
            log.info(f"Contatto creato: {info.email}  id={contact_id}")
            self._add_timeline_note(contact_id, info)
            return SyncResult(
                status=SyncStatus.CREATED,
                contact_email=info.email,
                contact_id=contact_id,
            )
        except ApiException as exc:
            # 409 = già esiste (race condition)
            if exc.status == 409:
                return self._handle_existing(info)
            log.error(f"Errore nella creazione del contatto {info.email}: {exc}")
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=info.email,
                contact_id=None,
                detail=f"errore API: {exc.status}",
            )

    # ── Aggiornamento ──────────────────────────────────────────────────────

    def update_contact(self, existing: dict, info: SenderInfo) -> SyncResult:
        """
        Aggiorna solo i campi HubSpot che risultano vuoti/None.
        Non sovrascrive dati già presenti.
        """
        contact_id: str = existing["id"]
        existing_props: dict = existing.get("properties", {})
        updates: dict[str, str] = {}

        def _missing(key: str) -> bool:
            val = existing_props.get(key)
            return not val or val.strip() == ""

        if _missing("firstname") and info.first_name:
            updates["firstname"] = info.first_name
        if _missing("lastname") and info.last_name:
            updates["lastname"] = info.last_name
        if _missing("company") and info.company_domain:
            updates["company"] = _company_from_domain(info.company_domain)
        if _missing("leadsource"):
            updates["leadsource"] = config.contact_source
        # Aggiunge sempre il tag come hs_lead_status se non già valorizzato
        if _missing("hs_lead_status"):
            updates["hs_lead_status"] = config.contact_tag

        if not updates:
            log.debug(f"Nessun aggiornamento necessario per {info.email}")
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=info.email,
                contact_id=contact_id,
                detail="già aggiornato",
            )

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=SimplePublicObjectInput(
                    properties=updates
                ),
            )
            log.info(f"Contatto aggiornato: {info.email}  id={contact_id}  campi={list(updates)}")
            self._add_timeline_note(contact_id, info)
            return SyncResult(
                status=SyncStatus.UPDATED,
                contact_email=info.email,
                contact_id=contact_id,
                detail=f"campi aggiornati: {', '.join(updates.keys())}",
            )
        except ApiException as exc:
            log.error(f"Errore nell'aggiornamento del contatto {info.email}: {exc}")
            return SyncResult(
                status=SyncStatus.IGNORED,
                contact_email=info.email,
                contact_id=contact_id,
                detail=f"errore API: {exc.status}",
            )

    # ── Timeline ───────────────────────────────────────────────────────────

    def _add_timeline_note(self, contact_id: str, info: SenderInfo) -> None:
        """
        Aggiunge un'attività 'Nota' alla timeline del contatto
        con i dettagli dell'email ricevuta.
        """
        try:
            note_body = (
                f"📧 Email in arrivo su Gmail\n"
                f"Da: {info.full_name or info.email} <{info.email}>\n"
                f"Oggetto: {info.subject}\n"
                f"Data: {info.date}\n"
                f"Tag: {config.contact_tag}"
            )
            # Usa l'API Engagements v1 per creare una nota
            self._client.api_client.call_api(
                "/engagements/v1/engagements",
                "POST",
                body={
                    "engagement": {
                        "active": True,
                        "type": "NOTE",
                    },
                    "associations": {
                        "contactIds": [int(contact_id)],
                    },
                    "metadata": {
                        "body": note_body,
                    },
                },
                response_type=object,
                auth_settings=["hapikey"],
                _return_http_data_only=True,
            )
            log.debug(f"Nota timeline aggiunta per il contatto {contact_id}")
        except Exception as exc:
            # Non bloccare il flusso principale per un errore di nota
            log.warning(f"Impossibile aggiungere la nota timeline per {contact_id}: {exc}")

    # ── Entry point ────────────────────────────────────────────────────────

    def sync_sender(self, info: SenderInfo) -> SyncResult:
        """
        Logica principale: cerca → crea o aggiorna.
        Questa è l'unica funzione che il sync engine deve chiamare.
        """
        existing = self.find_contact_by_email(info.email)
        if existing is None:
            return self.create_contact(info)
        return self.update_contact(existing, info)

    def _handle_existing(self, info: SenderInfo) -> SyncResult:
        """Recupera e aggiorna un contatto rilevato come duplicato al momento della creazione."""
        existing = self.find_contact_by_email(info.email)
        if existing:
            return self.update_contact(existing, info)
        return SyncResult(
            status=SyncStatus.IGNORED,
            contact_email=info.email,
            contact_id=None,
            detail="duplicato ma non trovato",
        )
