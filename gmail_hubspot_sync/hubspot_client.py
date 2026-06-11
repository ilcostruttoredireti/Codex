"""Client HubSpot via REST API per gestione contatti e note."""

from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# associationTypeId per note → contatto (HUBSPOT_DEFINED category = 202)
_NOTE_TO_CONTACT_TYPE_ID = 202


class HubSpotError(Exception):
    pass


class HubSpotClient:
    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Ricerca contatto
    # ------------------------------------------------------------------

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Cerca un contatto per email. Restituisce il record HubSpot o None."""
        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": [
                "email",
                "firstname",
                "lastname",
                "company",
                "hs_lead_source",
                "hs_analytics_source",
            ],
            "limit": 1,
        }
        r = self._session.post(f"{_BASE}/crm/v3/objects/contacts/search", json=payload)
        if r.status_code == 200:
            data = r.json()
            if data["total"] > 0:
                return data["results"][0]
            return None
        logger.error("HubSpot search error %s: %s", r.status_code, r.text[:200])
        return None

    # ------------------------------------------------------------------
    # Creazione contatto
    # ------------------------------------------------------------------

    def create_contact(self, properties: dict) -> Optional[str]:
        """Crea un nuovo contatto. Restituisce l'ID o None in caso di errore."""
        r = self._session.post(
            f"{_BASE}/crm/v3/objects/contacts",
            json={"properties": properties},
        )
        if r.status_code in (200, 201):
            return r.json()["id"]
        # 409 = contatto già esistente (race condition)
        if r.status_code == 409:
            logger.warning("Contatto già esistente (409) per %s", properties.get("email"))
            return self._extract_existing_id_from_409(r.json())
        logger.error("HubSpot create error %s: %s", r.status_code, r.text[:200])
        return None

    # ------------------------------------------------------------------
    # Aggiornamento contatto
    # ------------------------------------------------------------------

    def update_contact(self, contact_id: str, properties: dict) -> bool:
        """Aggiorna le proprietà di un contatto esistente."""
        if not properties:
            return True
        r = self._session.patch(
            f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": properties},
        )
        if r.ok:
            return True
        logger.error("HubSpot update error %s: %s", r.status_code, r.text[:200])
        return False

    # ------------------------------------------------------------------
    # Nota / attività
    # ------------------------------------------------------------------

    def create_note_for_contact(
        self,
        contact_id: str,
        note_body: str,
    ) -> Optional[str]:
        """Crea una nota HubSpot e la associa al contatto."""
        # 1. Crea la nota
        ts_ms = str(int(time.time() * 1000))
        r = self._session.post(
            f"{_BASE}/crm/v3/objects/notes",
            json={"properties": {"hs_note_body": note_body, "hs_timestamp": ts_ms}},
        )
        if not r.ok:
            logger.error("HubSpot note create error %s: %s", r.status_code, r.text[:200])
            return None
        note_id = r.json()["id"]

        # 2. Associa nota → contatto
        assoc_url = (
            f"{_BASE}/crm/v4/objects/notes/{note_id}"
            f"/associations/contacts/{contact_id}"
        )
        assoc_payload = [
            {
                "associationCategory": "HUBSPOT_DEFINED",
                "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
            }
        ]
        assoc_r = self._session.put(assoc_url, json=assoc_payload)
        if not assoc_r.ok:
            logger.warning(
                "Nota %s creata ma associazione fallita: %s", note_id, assoc_r.text[:200]
            )

        return note_id

    # ------------------------------------------------------------------
    # Utilità
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_existing_id_from_409(body: dict) -> Optional[str]:
        msg = body.get("message", "")
        # HubSpot include l'ID esistente nel messaggio: "Contact already exists. Existing ID: 12345"
        import re
        match = re.search(r"Existing ID:\s*(\d+)", msg)
        return match.group(1) if match else None

    @staticmethod
    def build_update_properties(contact_info, existing_props: dict) -> dict:
        """
        Costruisce il dict di aggiornamento con solo i campi vuoti nel contatto esistente.
        Non sovrascrive mai dati già presenti.
        """
        updates: dict = {}

        if contact_info.first_name and not existing_props.get("firstname"):
            updates["firstname"] = contact_info.first_name

        if contact_info.last_name and not existing_props.get("lastname"):
            updates["lastname"] = contact_info.last_name

        if contact_info.company and not existing_props.get("company"):
            updates["company"] = contact_info.company

        if not existing_props.get("hs_lead_source"):
            updates["hs_lead_source"] = "Gmail"

        return updates
