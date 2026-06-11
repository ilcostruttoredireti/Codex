"""HubSpot CRM API v3 client: contact create/update and timeline notes."""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config

log = logging.getLogger(__name__)

_HEADERS = lambda: {  # noqa: E731 – simple lambda for fresh copy each call
    "Authorization": f"Bearer {Config.HUBSPOT_ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def _session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=4, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


class HubSpotClient:
    BASE = Config.HUBSPOT_BASE_URL

    def __init__(self) -> None:
        self._s = _session()

    # ── Contact search ────────────────────────────────────────────────────────

    def find_contact_by_email(self, email: str) -> Optional[dict]:
        """Return the full contact dict {id, properties} or None."""
        url = f"{self.BASE}/crm/v3/objects/contacts/search"
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {"propertyName": "email", "operator": "EQ", "value": email}
                    ]
                }
            ],
            "properties": [
                "email", "firstname", "lastname", "company",
                "lifecyclestage", "hs_lead_status",
            ],
            "limit": 1,
        }
        resp = self._s.post(url, headers=_HEADERS(), json=body)
        resp.raise_for_status()
        results = resp.json().get("results", [])
        return results[0] if results else None

    # ── Contact create ────────────────────────────────────────────────────────

    def create_contact(
        self,
        email: str,
        firstname: str,
        lastname: str,
        company: str,
    ) -> dict:
        url = f"{self.BASE}/crm/v3/objects/contacts"
        props: dict[str, str] = {
            "email": email,
            "hs_lead_status": "NEW",
            "lifecyclestage": Config.NEW_CONTACT_LIFECYCLE,
        }
        if firstname:
            props["firstname"] = firstname
        if lastname:
            props["lastname"] = lastname
        if company:
            props["company"] = company

        resp = self._s.post(url, headers=_HEADERS(), json={"properties": props})
        resp.raise_for_status()
        return resp.json()

    # ── Contact update (fill missing fields only) ─────────────────────────────

    def update_contact_missing_fields(
        self,
        contact_id: str,
        existing_props: dict,
        firstname: str,
        lastname: str,
        company: str,
    ) -> bool:
        """PATCH only properties that are currently blank.  Returns True if any change was sent."""
        updates: dict[str, str] = {}
        if not existing_props.get("firstname") and firstname:
            updates["firstname"] = firstname
        if not existing_props.get("lastname") and lastname:
            updates["lastname"] = lastname
        if not existing_props.get("company") and company:
            updates["company"] = company

        if not updates:
            return False

        url = f"{self.BASE}/crm/v3/objects/contacts/{contact_id}"
        resp = self._s.patch(url, headers=_HEADERS(), json={"properties": updates})
        resp.raise_for_status()
        return True

    # ── Timeline note ─────────────────────────────────────────────────────────

    def add_inbound_gmail_note(
        self,
        contact_id: str,
        sender_email: str,
        subject: str,
        received_at: str,
        owner_id: Optional[int] = None,
    ) -> None:
        """Create a NOTE engagement and associate it with the contact."""
        timestamp_ms = _rfc2822_to_epoch_ms(received_at)

        note_body = (
            f"📥 <b>Inbound Gmail</b><br>"
            f"<b>From:</b> {sender_email}<br>"
            f"<b>Subject:</b> {subject or '(no subject)'}<br>"
            f"<b>Source:</b> Inbound Gmail<br>"
            f"<b>Tag:</b> Inbound Gmail"
        )

        props: dict = {
            "hs_note_body": note_body,
            "hs_timestamp": str(timestamp_ms),
        }
        if owner_id:
            props["hubspot_owner_id"] = str(owner_id)

        # Create note
        url_note = f"{self.BASE}/crm/v3/objects/notes"
        resp = self._s.post(url_note, headers=_HEADERS(), json={"properties": props})
        resp.raise_for_status()
        note_id = resp.json()["id"]

        # Associate note → contact (association type 202 = Note to Contact)
        url_assoc = (
            f"{self.BASE}/crm/v4/objects/notes/{note_id}"
            f"/associations/contacts/{contact_id}/202"
        )
        assoc_resp = self._s.put(url_assoc, headers=_HEADERS())
        if not assoc_resp.ok:
            log.warning(
                "Note %s created but association to contact %s failed: %s",
                note_id, contact_id, assoc_resp.text,
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _rfc2822_to_epoch_ms(date_str: str) -> int:
    """Parse an RFC-2822 date header into epoch milliseconds for HubSpot."""
    import email.utils
    if not date_str:
        return int(time.time() * 1000)
    try:
        ts = email.utils.parsedate_to_datetime(date_str)
        return int(ts.timestamp() * 1000)
    except Exception:
        return int(time.time() * 1000)
