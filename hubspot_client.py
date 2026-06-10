import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

import requests

from contact_extractor import ContactInfo


@dataclass
class SyncResult:
    status: str  # 'creato' | 'aggiornato' | 'ignorato'
    email: str
    contact_id: Optional[str] = None
    reason: Optional[str] = None


class HubSpotClient:
    _BASE = "https://api.hubapi.com"

    def __init__(self, access_token: str):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Contact operations
    # ------------------------------------------------------------------

    def find_contact(self, email: str) -> Optional[Dict[str, Any]]:
        """Return the first matching HubSpot contact or None."""
        resp = self._session.post(
            f"{self._BASE}/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "email",
                                "operator": "EQ",
                                "value": email,
                            }
                        ]
                    }
                ],
                "properties": [
                    "email",
                    "firstname",
                    "lastname",
                    "company",
                    "hs_lead_source",
                ],
                "limit": 1,
            },
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    def create_contact(self, props: Dict[str, str]) -> Dict[str, Any]:
        resp = self._session.post(
            f"{self._BASE}/crm/v3/objects/contacts",
            json={"properties": props},
        )
        resp.raise_for_status()
        return resp.json()

    def update_contact(
        self, contact_id: str, props: Dict[str, str]
    ) -> Dict[str, Any]:
        resp = self._session.patch(
            f"{self._BASE}/crm/v3/objects/contacts/{contact_id}",
            json={"properties": props},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Note / timeline activity
    # ------------------------------------------------------------------

    def create_note(
        self,
        contact_id: str,
        body: str,
        timestamp_ms: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Attach a timeline note to a contact."""
        ts = timestamp_ms or int(time.time() * 1000)
        resp = self._session.post(
            f"{self._BASE}/crm/v3/objects/notes",
            json={
                "properties": {
                    "hs_note_body": body,
                    "hs_timestamp": str(ts),
                },
                "associations": [
                    {
                        "to": {"id": contact_id},
                        "types": [
                            {
                                "associationCategory": "HUBSPOT_DEFINED",
                                "associationTypeId": 202,
                            }
                        ],
                    }
                ],
            },
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Main sync entry point
    # ------------------------------------------------------------------

    def sync_contact(
        self,
        info: ContactInfo,
        message: Optional[Dict[str, str]] = None,
    ) -> SyncResult:
        """
        Create or update a HubSpot contact from email sender info.
        Returns a SyncResult with status, email, and contact ID.
        """
        existing = self.find_contact(info.email)

        if existing:
            contact_id: str = existing["id"]
            current = existing.get("properties", {})

            updates: Dict[str, str] = {}
            if not current.get("firstname") and info.first_name:
                updates["firstname"] = info.first_name
            if not current.get("lastname") and info.last_name:
                updates["lastname"] = info.last_name
            if not current.get("company") and info.company:
                updates["company"] = info.company

            if updates:
                self.update_contact(contact_id, updates)
                status = "aggiornato"
            else:
                status = "ignorato"

            if status == "aggiornato" and message:
                self.create_note(
                    contact_id,
                    self._note_body(message),
                    _parse_timestamp_ms(message.get("date", "")),
                )

            return SyncResult(status=status, email=info.email, contact_id=contact_id)

        # --- New contact ---
        props: Dict[str, str] = {
            "email": info.email,
            "hs_lead_source": "Other Campaigns",
        }
        if info.first_name:
            props["firstname"] = info.first_name
        if info.last_name:
            props["lastname"] = info.last_name
        if info.company:
            props["company"] = info.company

        try:
            created = self.create_contact(props)
        except requests.HTTPError as exc:
            # 409 = contact already exists (race condition)
            if exc.response is not None and exc.response.status_code == 409:
                existing = self.find_contact(info.email)
                if existing:
                    return SyncResult(
                        status="ignorato",
                        email=info.email,
                        contact_id=existing["id"],
                        reason="race condition – already existed",
                    )
            raise

        contact_id = created["id"]

        if message:
            self.create_note(
                contact_id,
                self._note_body(message),
                _parse_timestamp_ms(message.get("date", "")),
            )

        return SyncResult(status="creato", email=info.email, contact_id=contact_id)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _note_body(message: Dict[str, str]) -> str:
        subject = message.get("subject") or "(nessun oggetto)"
        date = message.get("date") or ""
        return (
            "<b>[Inbound Gmail]</b> Email ricevuta<br>"
            f"Oggetto: {subject}<br>"
            f"Data: {date}<br>"
            "Fonte: Gmail"
        )


def _parse_timestamp_ms(date_str: str) -> Optional[int]:
    """Convert an RFC 2822 date string to Unix milliseconds."""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None
