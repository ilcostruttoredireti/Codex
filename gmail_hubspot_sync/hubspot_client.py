"""Client HubSpot: ricerca, creazione e aggiornamento contatti."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

import hubspot
from hubspot.crm.contacts import (
    ApiException,
    SimplePublicObjectInputForCreate,
    SimplePublicObjectInput,
)
from hubspot.crm.contacts.api import BasicApi, SearchApi
from hubspot.crm.contacts.models import (
    Filter,
    FilterGroup,
    PublicObjectSearchRequest,
)

from . import config

log = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    """Risultato dell'operazione di sync per un singolo contatto."""

    status: SyncStatus
    email: str
    contact_id: str = ""
    detail: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}]", f"email={self.email}"]
        if self.contact_id:
            parts.append(f"id={self.contact_id}")
        if self.detail:
            parts.append(f"({self.detail})")
        return " | ".join(parts)


def _company_from_domain(domain: str) -> str:
    """
    Ricava un nome azienda approssimativo dal dominio.
    Esempio: "mario.rossi@acme-corp.com" → "acme-corp.com"
    Esempio: "mail@gmail.com" → "" (dominio pubblico, non aziendale)
    """
    public_domains = {
        "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
        "outlook.com", "live.com", "icloud.com", "me.com", "mac.com",
        "libero.it", "tin.it", "alice.it", "virgilio.it", "tiscali.it",
        "protonmail.com", "proton.me", "tutanota.com", "fastmail.com",
    }
    return "" if domain.lower() in public_domains else domain.lower()


class HubSpotClient:
    """
    Wrapper intorno all'API HubSpot CRM Contacts v3.

    Usa HubSpot Private App Token (Bearer).
    """

    def __init__(self) -> None:
        self._client = hubspot.Client.create(
            access_token=config.HUBSPOT_ACCESS_TOKEN
        )
        self._contacts_api: BasicApi = self._client.crm.contacts.basic_api
        self._search_api: SearchApi = self._client.crm.contacts.search_api

    # ── Ricerca ─────────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> dict[str, Any] | None:
        """
        Cerca un contatto per email (chiave unica).
        Restituisce il contatto come dict (properties + id) o None.
        """
        search_request = PublicObjectSearchRequest(
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
            properties=["email", "firstname", "lastname", "company", "hs_lead_status"],
            limit=1,
        )
        try:
            result = self._search_api.do_search(
                public_object_search_request=search_request
            )
            if result.total > 0:
                contact = result.results[0]
                return {
                    "id": contact.id,
                    "properties": contact.properties,
                }
        except ApiException as exc:
            log.error("Errore ricerca HubSpot per %s: %s", email, exc)
        return None

    # ── Creazione ───────────────────────────────────────────────────────────

    def create_contact(self, properties: dict[str, str]) -> str:
        """
        Crea un nuovo contatto. Restituisce l'ID del contatto creato.
        Lancia ApiException in caso di errore.
        """
        obj = SimplePublicObjectInputForCreate(properties=properties)
        contact = self._contacts_api.create(
            simple_public_object_input_for_create=obj
        )
        return contact.id

    # ── Aggiornamento ───────────────────────────────────────────────────────

    def update_contact(self, contact_id: str, properties: dict[str, str]) -> None:
        """
        Aggiorna un contatto esistente con le proprietà specificate.
        Aggiorna solo i campi vuoti (non sovrascrive dati esistenti).
        """
        obj = SimplePublicObjectInput(properties=properties)
        self._contacts_api.update(
            contact_id=contact_id,
            simple_public_object_input=obj,
        )

    # ── Sync principale ──────────────────────────────────────────────────────

    def sync_sender(
        self,
        email: str,
        first_name: str = "",
        last_name: str = "",
        display_name: str = "",
        domain: str = "",
        subject: str = "",
        message_id: str = "",
    ) -> SyncResult:
        """
        Sincronizza un mittente email con HubSpot:
        - Cerca per email
        - Se non esiste → crea
        - Se esiste → aggiorna i campi vuoti

        Restituisce un SyncResult con stato e ID contatto.
        """
        company = _company_from_domain(domain) if domain else ""

        # Proprietà base da impostare
        base_props = {
            "email": email.lower(),
            "leadsource": config.CONTACT_SOURCE,
        }
        if first_name:
            base_props["firstname"] = first_name
        if last_name:
            base_props["lastname"] = last_name
        if company:
            base_props["company"] = company

        # Cerca contatto esistente
        existing = self.find_contact_by_email(email)

        if existing is None:
            # ── CREA ────────────────────────────────────────────────────────
            try:
                contact_id = self.create_contact(base_props)
                log.info("Contatto CREATO: %s (id=%s)", email, contact_id)
                return SyncResult(
                    status=SyncStatus.CREATED,
                    email=email,
                    contact_id=contact_id,
                )
            except ApiException as exc:
                log.error("Errore creazione contatto %s: %s", email, exc)
                return SyncResult(
                    status=SyncStatus.ERROR,
                    email=email,
                    detail=str(exc.reason or exc),
                )

        # ── AGGIORNA ────────────────────────────────────────────────────────
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        # Aggiorna solo i campi attualmente vuoti in HubSpot
        update_props: dict[str, str] = {}
        for prop, value in base_props.items():
            if prop == "email":
                continue  # non modificare l'email chiave
            current = existing_props.get(prop, "") or ""
            if not current.strip() and value:
                update_props[prop] = value

        if update_props:
            try:
                self.update_contact(contact_id, update_props)
                fields_updated = ", ".join(update_props.keys())
                log.info(
                    "Contatto AGGIORNATO: %s (id=%s) → campi: %s",
                    email, contact_id, fields_updated,
                )
                return SyncResult(
                    status=SyncStatus.UPDATED,
                    email=email,
                    contact_id=contact_id,
                    detail=f"campi: {fields_updated}",
                )
            except ApiException as exc:
                log.error("Errore aggiornamento contatto %s: %s", email, exc)
                return SyncResult(
                    status=SyncStatus.ERROR,
                    email=email,
                    contact_id=contact_id,
                    detail=str(exc.reason or exc),
                )
        else:
            log.info("Contatto già aggiornato, nessuna modifica: %s (id=%s)", email, contact_id)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=email,
                contact_id=contact_id,
                detail="dati già presenti",
            )
