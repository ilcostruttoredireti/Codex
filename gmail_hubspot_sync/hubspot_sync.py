"""
Modulo HubSpot: ricerca, creazione e aggiornamento contatti via API v3.
Aggiunge timeline activity e tag 'Inbound Gmail'.
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

from config import (
    CONTACT_SOURCE_LABEL,
    CONTACT_TAG,
    HUBSPOT_API_KEY,
    HUBSPOT_BASE_URL,
    IGNORED_DOMAINS,
)
from gmail_monitor import SenderInfo

logger = logging.getLogger(__name__)


class SyncStatus(Enum):
    CREATO    = "Creato"
    AGGIORNATO = "Aggiornato"
    IGNORATO  = "Ignorato"
    ERRORE    = "Errore"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str] = None
    reason: str = ""


class HubSpotSync:
    def __init__(self, api_key: str = HUBSPOT_API_KEY):
        if not api_key:
            raise ValueError(
                "HUBSPOT_API_KEY non impostata.\n"
                "Imposta la variabile d'ambiente o aggiungila al file .env"
            )
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        url = f"{HUBSPOT_BASE_URL}{path}"
        resp = requests.get(url, headers=self._headers, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, payload: dict) -> dict:
        url = f"{HUBSPOT_BASE_URL}{path}"
        resp = requests.post(url, headers=self._headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, payload: dict) -> dict:
        url = f"{HUBSPOT_BASE_URL}{path}"
        resp = requests.patch(url, headers=self._headers, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    # ── Contact lookup ───────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """
        Cerca un contatto per email.
        Restituisce il record contatto o None se non trovato.
        """
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": email.lower(),
                        }
                    ]
                }
            ],
            "properties": [
                "email", "firstname", "lastname", "company",
                "hs_lead_status", "leadsource", "hs_tag_ids",
            ],
            "limit": 1,
        }
        try:
            result = self._post("/crm/v3/objects/contacts/search", payload)
            results = result.get("results", [])
            return results[0] if results else None
        except requests.HTTPError as e:
            logger.error("Errore ricerca contatto %s: %s", email, e)
            return None

    # ── Contact create / update ───────────────────────────────────────────────

    def _build_properties(self, sender: SenderInfo, existing: Optional[dict] = None) -> dict:
        """
        Costruisce il dizionario proprietà per HubSpot.
        Se il contatto esiste, aggiorna solo i campi mancanti (blank).
        """
        props = {}
        ex_props = existing.get("properties", {}) if existing else {}

        def _set_if_missing(hs_key: str, value: str):
            """Imposta il valore solo se il campo è vuoto nell'esistente."""
            current = ex_props.get(hs_key, "") or ""
            if not current.strip() and value:
                props[hs_key] = value

        if existing:
            # Aggiornamento: riempi solo i campi vuoti
            _set_if_missing("firstname", sender.first_name)
            _set_if_missing("lastname", sender.last_name)
            _set_if_missing("company", sender.company)
        else:
            # Creazione: imposta tutti i campi disponibili
            if sender.first_name:
                props["firstname"] = sender.first_name
            if sender.last_name:
                props["lastname"] = sender.last_name
            if sender.company:
                props["company"] = sender.company
            props["email"] = sender.email.lower()
            props["leadsource"] = CONTACT_SOURCE_LABEL   # "Gmail"

        return props

    def create_contact(self, sender: SenderInfo) -> Optional[str]:
        """Crea un nuovo contatto. Restituisce l'ID o None in caso di errore."""
        props = self._build_properties(sender)
        try:
            result = self._post("/crm/v3/objects/contacts", {"properties": props})
            contact_id = result.get("id")
            logger.info("Contatto creato → ID %s (%s)", contact_id, sender.email)
            return contact_id
        except requests.HTTPError as e:
            logger.error("Errore creazione contatto %s: %s", sender.email, e)
            return None

    def update_contact(self, contact_id: str, sender: SenderInfo, existing: dict) -> bool:
        """
        Aggiorna i campi vuoti di un contatto esistente.
        Restituisce True se almeno un campo è stato aggiornato.
        """
        props = self._build_properties(sender, existing=existing)
        if not props:
            logger.debug("Nessun campo da aggiornare per %s (ID %s)", sender.email, contact_id)
            return False
        try:
            self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": props})
            logger.info(
                "Contatto aggiornato → ID %s (%s) — campi: %s",
                contact_id, sender.email, list(props.keys()),
            )
            return True
        except requests.HTTPError as e:
            logger.error("Errore aggiornamento contatto %s: %s", contact_id, e)
            return False

    # ── Tag ──────────────────────────────────────────────────────────────────

    def add_tag_to_contact(self, contact_id: str, tag: str = CONTACT_TAG):
        """
        HubSpot non ha tag nativi sui contatti → usiamo una proprietà
        'hs_additional_emails' o una custom property come note.
        Strategia: aggiunge il tag nella nota/description se non già presente.

        Se preferisci tag ufficiali, attiva HubSpot Tags nelle impostazioni
        e usa l'API /crm/v3/objects/contacts/{id}/associations.
        """
        try:
            contact = self._get(f"/crm/v3/objects/contacts/{contact_id}",
                                params={"properties": "hs_content_membership_notes"})
            existing_note = contact.get("properties", {}).get("hs_content_membership_notes") or ""
            if tag not in existing_note:
                new_note = f"{existing_note}\n{tag}".strip() if existing_note else tag
                self._patch(
                    f"/crm/v3/objects/contacts/{contact_id}",
                    {"properties": {"hs_content_membership_notes": new_note}},
                )
                logger.debug("Tag '%s' aggiunto al contatto %s", tag, contact_id)
        except requests.HTTPError as e:
            logger.warning("Impossibile aggiungere tag al contatto %s: %s", contact_id, e)

    # ── Timeline activity ─────────────────────────────────────────────────────

    def log_email_activity(self, contact_id: str, sender: SenderInfo):
        """
        Crea un'attività 'Email' nella timeline del contatto.
        Usa le Note API come fallback (non richiede configurazione extra).
        """
        note_body = (
            f"📧 Email ricevuta da Gmail\n"
            f"Da: {sender.raw_from}\n"
            f"Oggetto: {sender.subject or '(nessun oggetto)'}\n"
            f"Data: {sender.date or 'N/D'}\n"
            f"Tag: {CONTACT_TAG}"
        )
        payload = {
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
                            "associationTypeId": 202,  # Note → Contact
                        }
                    ],
                }
            ],
        }
        try:
            self._post("/crm/v3/objects/notes", payload)
            logger.debug("Activity nota creata per contatto %s", contact_id)
        except requests.HTTPError as e:
            logger.warning("Impossibile creare activity per %s: %s", contact_id, e)

    # ── Main sync entry point ─────────────────────────────────────────────────

    def sync_sender(self, sender: SenderInfo) -> SyncResult:
        """
        Logica principale: cerca il contatto, crea o aggiorna.
        """
        email = sender.email.lower()

        # ── Filtro domini personali ──────────────────────────────────────────
        if sender.domain in IGNORED_DOMAINS:
            return SyncResult(
                status=SyncStatus.IGNORATO,
                email=email,
                reason=f"dominio personale ignorato ({sender.domain})",
            )

        # ── Cerca contatto esistente ─────────────────────────────────────────
        existing = self.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            updated = self.update_contact(contact_id, sender, existing)
            self.add_tag_to_contact(contact_id)
            self.log_email_activity(contact_id, sender)
            return SyncResult(
                status=SyncStatus.AGGIORNATO if updated else SyncStatus.IGNORATO,
                email=email,
                contact_id=contact_id,
                reason="" if updated else "nessun campo vuoto da aggiornare",
            )

        # ── Crea nuovo contatto ──────────────────────────────────────────────
        contact_id = self.create_contact(sender)
        if not contact_id:
            return SyncResult(
                status=SyncStatus.ERRORE,
                email=email,
                reason="errore API HubSpot in fase di creazione",
            )

        self.add_tag_to_contact(contact_id)
        self.log_email_activity(contact_id, sender)
        return SyncResult(
            status=SyncStatus.CREATO,
            email=email,
            contact_id=contact_id,
        )


# ── Utility ───────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    """Timestamp corrente in millisecondi (richiesto da HubSpot)."""
    import time
    return int(time.time() * 1000)
