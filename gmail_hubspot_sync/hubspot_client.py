import logging
from datetime import datetime, timezone
from typing import Optional

import requests

from .models import SenderInfo

logger = logging.getLogger(__name__)

_BASE = "https://api.hubapi.com"

# Tipo associazione HubSpot v4: email → contatto
_ASSOC_EMAIL_TO_CONTACT = [
    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 198}
]
_ASSOC_NOTE_TO_CONTACT = [
    {"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}
]


class HubSpotClient:
    def __init__(self, access_token: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ──────────────────────────────────────────────
    # HTTP helpers
    # ──────────────────────────────────────────────

    def _get(self, path: str, **kwargs) -> Optional[dict]:
        try:
            r = self.session.get(f"{_BASE}{path}", **kwargs)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            logger.error(f"HubSpot GET {path} → {e.response.status_code}: {e.response.text[:300]}")
            return None

    def _post(self, path: str, json: dict) -> Optional[dict]:
        try:
            r = self.session.post(f"{_BASE}{path}", json=json)
            if r.status_code == 409:
                # Conflitto: contatto già esistente (race condition)
                return {"_conflict": True, "response": r.json()}
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            logger.error(f"HubSpot POST {path} → {e.response.status_code}: {e.response.text[:300]}")
            return None

    def _patch(self, path: str, json: dict) -> Optional[dict]:
        try:
            r = self.session.patch(f"{_BASE}{path}", json=json)
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            logger.error(f"HubSpot PATCH {path} → {e.response.status_code}: {e.response.text[:300]}")
            return None

    def _put(self, path: str, json: dict) -> Optional[dict]:
        try:
            r = self.session.put(f"{_BASE}{path}", json=json)
            r.raise_for_status()
            return r.json() if r.content else {}
        except requests.HTTPError as e:
            logger.error(f"HubSpot PUT {path} → {e.response.status_code}: {e.response.text[:300]}")
            return None

    # ──────────────────────────────────────────────
    # Contatti
    # ──────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        result = self._post(
            "/crm/v3/objects/contacts/search",
            {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email", "firstname", "lastname", "company", "hs_lead_source"],
                "limit": 1,
            },
        )
        if result and result.get("results"):
            return result["results"][0]
        return None

    def create_contact(self, sender: SenderInfo) -> Optional[str]:
        props = self._build_props(sender)
        result = self._post("/crm/v3/objects/contacts", {"properties": props})

        if result is None:
            return None

        # Gestione conflitto 409 (contatto già esistente)
        if result.get("_conflict"):
            logger.warning(f"Conflitto 409 per {sender.email}: recupero ID esistente.")
            existing = self.find_contact_by_email(sender.email)
            return existing["id"] if existing else None

        return result.get("id")

    def update_contact_missing_fields(
        self, contact_id: str, sender: SenderInfo, existing: dict
    ) -> str:
        ep = existing.get("properties", {})
        updates: dict[str, str] = {}

        if not ep.get("firstname") and sender.firstname:
            updates["firstname"] = sender.firstname
        if not ep.get("lastname") and sender.lastname:
            updates["lastname"] = sender.lastname
        if not ep.get("company") and sender.company:
            updates["company"] = sender.company

        if updates:
            self._patch(f"/crm/v3/objects/contacts/{contact_id}", {"properties": updates})
            logger.debug(f"Aggiornati campi {list(updates.keys())} per contatto {contact_id}")

        return contact_id

    def _build_props(self, sender: SenderInfo) -> dict:
        props: dict[str, str] = {
            "email": sender.email,
            "hs_lead_source": "EMAIL_MARKETING",
        }
        if sender.firstname:
            props["firstname"] = sender.firstname
        if sender.lastname:
            props["lastname"] = sender.lastname
        if sender.company:
            props["company"] = sender.company
        return props

    # ──────────────────────────────────────────────
    # Engagement email (timeline attività)
    # ──────────────────────────────────────────────

    def create_email_engagement(
        self, contact_id: str, sender: SenderInfo
    ) -> Optional[str]:
        ts_ms = int(
            (sender.received_at or datetime.now(timezone.utc)).timestamp() * 1000
        )
        result = self._post(
            "/crm/v3/objects/emails",
            {
                "properties": {
                    "hs_timestamp": str(ts_ms),
                    "hs_email_direction": "INCOMING_EMAIL",
                    "hs_email_status": "RECEIVED",
                    "hs_email_subject": sender.subject or "(nessun oggetto)",
                    "hs_email_text": f"Email ricevuta da: {sender.raw_from}",
                    "hs_email_sender_email": sender.email,
                }
            },
        )
        if not result or result.get("_conflict"):
            return None

        email_obj_id = result.get("id")
        if email_obj_id:
            self._put(
                f"/crm/v4/objects/emails/{email_obj_id}/associations/contacts/{contact_id}",
                _ASSOC_EMAIL_TO_CONTACT,
            )
        return email_obj_id

    # ──────────────────────────────────────────────
    # Nota di tag
    # ──────────────────────────────────────────────

    def add_tag_note(self, contact_id: str, sender: SenderInfo) -> Optional[str]:
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        note_body = (
            f"Tag: Inbound Gmail\n"
            f"Fonte: Gmail\n"
            f"Email ricevuta: {sender.subject}\n"
            f"Da: {sender.raw_from}"
        )
        result = self._post(
            "/crm/v3/objects/notes",
            {
                "properties": {
                    "hs_timestamp": str(ts_ms),
                    "hs_note_body": note_body,
                }
            },
        )
        if not result or result.get("_conflict"):
            return None

        note_id = result.get("id")
        if note_id:
            self._put(
                f"/crm/v4/objects/notes/{note_id}/associations/contacts/{contact_id}",
                _ASSOC_NOTE_TO_CONTACT,
            )
        return note_id
