"""
Sincronizzazione contatti su HubSpot.
Usa l'email come chiave univoca: crea il contatto se assente, altrimenti aggiorna i campi vuoti.
"""

import logging
from dataclasses import dataclass
from enum import Enum
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

from contact_parser import SenderContact

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str] = None
    detail: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}]", self.email]
        if self.contact_id:
            parts.append(f"ID={self.contact_id}")
        if self.detail:
            parts.append(f"({self.detail})")
        return " | ".join(parts)


# Indirizzi da non importare (sistemi, noreply, ecc.)
_SKIP_PREFIXES = ("noreply", "no-reply", "donotreply", "do-not-reply",
                  "mailer-daemon", "postmaster", "bounce", "notifications",
                  "newsletter", "info@", "support@", "help@")


class HubSpotSync:
    """Gestisce la sincronizzazione dei contatti Gmail su HubSpot."""

    def __init__(self, access_token: str, contact_source: str = "Gmail"):
        self._client = hubspot.Client.create(access_token=access_token)
        self._contact_source = contact_source

    # ------------------------------------------------------------------
    # Punto di ingresso principale
    # ------------------------------------------------------------------

    def sync_contact(self, sender: SenderContact) -> SyncResult:
        """Sincronizza un mittente Gmail su HubSpot. Restituisce il risultato dell'operazione."""
        email = sender.email

        if self._should_skip(email):
            return SyncResult(SyncStatus.IGNORED, email, detail="indirizzo automatico")

        existing = self._find_contact_by_email(email)

        if existing:
            return self._update_if_needed(existing, sender)
        else:
            return self._create_contact(sender)

    # ------------------------------------------------------------------
    # Ricerca
    # ------------------------------------------------------------------

    def _find_contact_by_email(self, email: str) -> Optional[dict]:
        """Cerca un contatto per email. Restituisce il record HubSpot o None."""
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
            properties=["email", "firstname", "lastname", "company",
                         "hs_lead_status", "lead_source"],
            limit=1,
        )
        try:
            response = self._client.crm.contacts.search_api.do_search(
                public_object_search_request=search_request
            )
            if response.total > 0:
                return response.results[0]
        except ApiException as exc:
            logger.error("Errore ricerca contatto %s: %s", email, exc)
        return None

    # ------------------------------------------------------------------
    # Creazione
    # ------------------------------------------------------------------

    def _create_contact(self, sender: SenderContact) -> SyncResult:
        props = self._build_properties(sender, existing_props={})
        props["hs_lead_status"] = "NEW"

        try:
            response = self._client.crm.contacts.basic_api.create(
                simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
                    properties=props
                )
            )
            contact_id = str(response.id)
            logger.info("Contatto creato: %s (ID=%s)", sender.email, contact_id)
            return SyncResult(SyncStatus.CREATED, sender.email, contact_id)
        except ApiException as exc:
            logger.error("Errore creazione contatto %s: %s", sender.email, exc)
            return SyncResult(SyncStatus.IGNORED, sender.email, detail=f"errore API: {exc.status}")

    # ------------------------------------------------------------------
    # Aggiornamento
    # ------------------------------------------------------------------

    def _update_if_needed(self, existing: object, sender: SenderContact) -> SyncResult:
        contact_id = str(existing.id)
        existing_props = existing.properties or {}

        updates = self._build_properties(sender, existing_props=existing_props)

        # Rimuovi i campi già valorizzati nel record esistente
        updates = {k: v for k, v in updates.items()
                   if v and not existing_props.get(k)}

        if not updates:
            return SyncResult(SyncStatus.IGNORED, sender.email, contact_id,
                               detail="nessun campo da aggiornare")

        try:
            self._client.crm.contacts.basic_api.update(
                contact_id=contact_id,
                simple_public_object_input=hubspot.crm.contacts.models.SimplePublicObjectInput(
                    properties=updates
                ),
            )
            logger.info("Contatto aggiornato: %s (ID=%s) campi=%s",
                        sender.email, contact_id, list(updates.keys()))
            return SyncResult(SyncStatus.UPDATED, sender.email, contact_id,
                              detail=f"campi: {', '.join(updates.keys())}")
        except ApiException as exc:
            logger.error("Errore aggiornamento contatto %s: %s", sender.email, exc)
            return SyncResult(SyncStatus.IGNORED, sender.email, contact_id,
                              detail=f"errore API: {exc.status}")

    # ------------------------------------------------------------------
    # Costruzione proprietà HubSpot
    # ------------------------------------------------------------------

    def _build_properties(self, sender: SenderContact, existing_props: dict) -> dict:
        props: dict[str, str] = {}

        if sender.email:
            props["email"] = sender.email
        if sender.first_name:
            props["firstname"] = sender.first_name
        if sender.last_name:
            props["lastname"] = sender.last_name
        if sender.company:
            props["company"] = sender.company

        props["lead_source"] = self._contact_source

        # Tag "Inbound Gmail" nel campo hs_content_membership_notes o note
        props["hs_note_body"] = f"Tag: Inbound {self._contact_source}"

        return props

    # ------------------------------------------------------------------
    # Utilità
    # ------------------------------------------------------------------

    @staticmethod
    def _should_skip(email: str) -> bool:
        email_lower = email.lower()
        return any(email_lower.startswith(p) or (p.endswith("@") and p in email_lower)
                   for p in _SKIP_PREFIXES)
