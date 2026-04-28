import logging
import time
from typing import Optional

import requests

import config
from models import SenderInfo

logger = logging.getLogger(__name__)

_CONTACTS_URL = f"{config.HUBSPOT_BASE_URL}/crm/v3/objects/contacts"
_NOTES_URL = f"{config.HUBSPOT_BASE_URL}/crm/v3/objects/notes"

# HubSpot HUBSPOT_DEFINED association type: Note → Contact
_NOTE_TO_CONTACT_TYPE_ID = 202


class HubSpotClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.HUBSPOT_API_KEY}",
                "Content-Type": "application/json",
            }
        )

    # ── Internal request helper ───────────────────────────────────────────────

    def _request(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(5):
            resp = self._session.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 10))
                logger.warning("Rate limit HubSpot, attesa %ss (tentativo %s)", wait, attempt + 1)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        raise RuntimeError(f"Request failed after retries: {method} {url}")

    # ── Contact operations ────────────────────────────────────────────────────

    def find_contact(self, email: str) -> Optional[dict]:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": ["email", "firstname", "lastname", "company"],
            "limit": 1,
        }
        result = self._request("POST", f"{_CONTACTS_URL}/search", json=body)
        results = result.get("results", [])
        return results[0] if results else None

    def create_contact(self, sender: SenderInfo) -> dict:
        props = _build_create_props(sender)
        return self._request("POST", _CONTACTS_URL, json={"properties": props})

    def update_contact(self, contact_id: str, sender: SenderInfo, existing_props: dict) -> dict:
        """Patch only fields that are currently empty in HubSpot."""
        props = _build_update_props(sender, existing_props)
        if not props:
            return {}
        return self._request(
            "PATCH", f"{_CONTACTS_URL}/{contact_id}", json={"properties": props}
        )

    # ── Timeline activity ─────────────────────────────────────────────────────

    def create_email_note(self, contact_id: str, sender: SenderInfo) -> None:
        """Create a Note engagement linked to the contact's timeline."""
        body_text = (
            f"📧 Email ricevuta da {sender.name or sender.email}\n"
            f"Mittente: {sender.email}\n"
            f"Dominio: {sender.domain}\n"
            f"Azienda: {sender.company or '—'}\n"
            f"Tag: Inbound Gmail"
        )
        try:
            self._request(
                "POST",
                _NOTES_URL,
                json={
                    "properties": {
                        "hs_note_body": body_text,
                        "hs_timestamp": str(int(time.time() * 1000)),
                    },
                    "associations": [
                        {
                            "to": {"id": int(contact_id)},
                            "types": [
                                {
                                    "associationCategory": "HUBSPOT_DEFINED",
                                    "associationTypeId": _NOTE_TO_CONTACT_TYPE_ID,
                                }
                            ],
                        }
                    ],
                },
            )
        except Exception:
            logger.warning(
                "Impossibile creare la nota per il contatto %s — ignorato", contact_id
            )


# ── Property builders ─────────────────────────────────────────────────────────

def _build_create_props(sender: SenderInfo) -> dict:
    props: dict = {"email": sender.email}
    if sender.first_name:
        props["firstname"] = sender.first_name
    if sender.last_name:
        props["lastname"] = sender.last_name
    if sender.company:
        props["company"] = sender.company
    return props


def _build_update_props(sender: SenderInfo, existing: dict) -> dict:
    """Return only the fields that are missing in the existing HubSpot contact."""
    props: dict = {}
    if not existing.get("firstname") and sender.first_name:
        props["firstname"] = sender.first_name
    if not existing.get("lastname") and sender.last_name:
        props["lastname"] = sender.last_name
    if not existing.get("company") and sender.company:
        props["company"] = sender.company
    return props
