"""HubSpot REST client — contact lookup, create/update, note creation."""
import logging
from datetime import datetime, timezone

import requests

import config

HUBSPOT_BASE = "https://api.hubapi.com"

log = logging.getLogger(__name__)


class HubSpotManager:
    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {config.HUBSPOT_ACCESS_TOKEN}",
                "Content-Type": "application/json",
            }
        )

    # ── contact search ────────────────────────────────────────────────────────

    def find_by_email(self, email: str) -> dict | None:
        """Return {'id': str, 'properties': dict} or None."""
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/search"
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
        resp = self._session.post(url, json=body)
        if not resp.ok:
            log.error("HubSpot search failed %s: %s", resp.status_code, resp.text[:200])
            return None

        results = resp.json().get("results", [])
        if results:
            c = results[0]
            return {"id": c["id"], "properties": c.get("properties", {})}
        return None

    # ── contact create ────────────────────────────────────────────────────────

    def create_contact(self, contact: dict) -> str | None:
        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts"
        props: dict[str, str] = {"email": contact["email"]}

        for field in ("firstname", "lastname", "company"):
            if contact.get(field):
                props[field] = contact[field]

        # Best-effort: lead source (exists in most portals)
        props["hs_lead_source"] = "Gmail"

        resp = self._session.post(url, json={"properties": props})
        if resp.ok:
            return resp.json().get("id")

        log.error("HubSpot create failed %s: %s", resp.status_code, resp.text[:200])
        return None

    # ── contact update ────────────────────────────────────────────────────────

    def update_contact(
        self, contact_id: str, contact: dict, existing_props: dict
    ) -> bool:
        updates = {
            field: contact[field]
            for field in ("firstname", "lastname", "company")
            if contact.get(field) and not existing_props.get(field)
        }
        if not updates:
            return False

        url = f"{HUBSPOT_BASE}/crm/v3/objects/contacts/{contact_id}"
        resp = self._session.patch(url, json={"properties": updates})
        if resp.ok:
            return True

        log.error("HubSpot update failed %s: %s", resp.status_code, resp.text[:200])
        return False

    # ── timeline note ─────────────────────────────────────────────────────────

    def create_note(self, contact_id: str, body: str) -> str | None:
        """Create an engagement note and associate it with the contact."""
        ts_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # 1. Create note object
        url = f"{HUBSPOT_BASE}/crm/v3/objects/notes"
        resp = self._session.post(
            url,
            json={
                "properties": {
                    "hs_timestamp": str(ts_ms),
                    "hs_note_body": body,
                }
            },
        )
        if not resp.ok:
            log.warning("HubSpot note create failed %s: %s", resp.status_code, resp.text[:200])
            return None

        note_id = resp.json().get("id")

        # 2. Associate note → contact (type 202 = note-to-contact, HubSpot defined)
        assoc_url = (
            f"{HUBSPOT_BASE}/crm/v4/objects/notes/{note_id}"
            f"/associations/contacts/{contact_id}"
        )
        assoc_resp = self._session.put(
            assoc_url,
            json=[{"associationCategory": "HUBSPOT_DEFINED", "associationTypeId": 202}],
        )
        if not assoc_resp.ok:
            log.warning(
                "Note created (id=%s) but association failed: %s",
                note_id,
                assoc_resp.status_code,
            )

        return note_id
