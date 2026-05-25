"""HubSpot CRM API client for contact management."""

import logging
import time
from typing import Any, Dict, Optional

import requests

from .config import HubSpotConfig
from .models import ContactInfo, SyncResult, SyncStatus

logger = logging.getLogger(__name__)


class HubSpotClient:
    """Create and update HubSpot contacts via the v3 CRM API."""

    # HubSpot rate-limit: 110 req/10s for private apps
    _RETRY_BACKOFF = [1, 2, 4]  # seconds between retries

    def __init__(self, config: HubSpotConfig):
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {config.access_token}",
                "Content-Type": "application/json",
            }
        )

    # ---------------------------------------------------------------- helpers

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """Make a request with retry on 429 / transient errors."""
        url = f"{self.config.base_url}{path}"
        last_exc = None
        for delay in [0] + self._RETRY_BACKOFF:
            if delay:
                time.sleep(delay)
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", delay + 1))
                    logger.warning("HubSpot rate limit — attendo %ds", retry_after)
                    time.sleep(retry_after)
                    continue
                return resp
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("Errore di rete HubSpot: %s", exc)
        raise ConnectionError(f"HubSpot non raggiungibile dopo 4 tentativi") from last_exc

    # ---------------------------------------------------------------- search

    def find_contact_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        """
        Search HubSpot for a contact by email.

        Returns the contact dict (with id + properties) or None.
        """
        payload = {
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
                "email", "firstname", "lastname", "company",
                "hs_lead_status", "lifecyclestage", "hs_tag",
            ],
            "limit": 1,
        }
        resp = self._request(
            "POST", "/crm/v3/objects/contacts/search", json=payload
        )
        if resp.status_code != 200:
            logger.error("Errore ricerca HubSpot: %s %s", resp.status_code, resp.text)
            return None

        results = resp.json().get("results", [])
        return results[0] if results else None

    # ---------------------------------------------------------------- create

    def _build_properties(
        self, contact: ContactInfo, existing: Optional[Dict[str, Any]] = None
    ) -> Dict[str, str]:
        """
        Build a HubSpot properties dict for a contact.

        If `existing` is provided, only include fields that are blank in HubSpot.
        """
        existing_props = (existing or {}).get("properties", {})
        props: Dict[str, str] = {}

        def _set(hs_field: str, value: Optional[str]):
            """Only set if value is non-empty and (field is missing or empty in HS)."""
            if value and not existing_props.get(hs_field):
                props[hs_field] = value

        _set("email", contact.email)
        _set("firstname", contact.first_name)
        _set("lastname", contact.last_name)
        _set("company", contact.company)

        # Always mark source
        if not existing_props.get("hs_analytics_source"):
            props["hs_analytics_source"] = "OTHER_CAMPAIGNS"

        # Lead source / custom field (many portals use 'hs_lead_status' or custom props)
        # We use a note-style field; if you have a custom property, adjust here.
        if not existing_props.get("leadsource"):
            props["leadsource"] = self.config.contact_source  # "Gmail"

        return props

    def create_contact(self, contact: ContactInfo) -> SyncResult:
        """Create a new contact in HubSpot."""
        props = self._build_properties(contact)
        props["email"] = contact.email  # always include

        payload = {"properties": props}
        resp = self._request("POST", "/crm/v3/objects/contacts", json=payload)

        if resp.status_code in (200, 201):
            data = resp.json()
            contact_id = data.get("id")
            logger.info("Contatto CREATO in HubSpot: %s (ID: %s)", contact.email, contact_id)
            # Add tag note via engagement
            self._add_note(contact_id, contact.email)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=contact.email,
                hubspot_contact_id=contact_id,
            )

        elif resp.status_code == 409:
            # Conflict — contact already exists (race condition)
            logger.warning("Conflitto creazione per %s — riprovo come update", contact.email)
            existing = self.find_contact_by_email(contact.email)
            if existing:
                return self.update_contact(contact, existing)
            return SyncResult(
                status=SyncStatus.ERROR,
                email=contact.email,
                error="Conflitto 409, contatto non trovato in seguito",
            )

        else:
            logger.error(
                "Errore creazione HubSpot: %s %s", resp.status_code, resp.text
            )
            return SyncResult(
                status=SyncStatus.ERROR,
                email=contact.email,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

    # ---------------------------------------------------------------- update

    def update_contact(
        self, contact: ContactInfo, existing: Dict[str, Any]
    ) -> SyncResult:
        """Update an existing HubSpot contact with any missing fields."""
        contact_id = existing["id"]
        props = self._build_properties(contact, existing)

        if not props:
            # Nothing new to update
            logger.info("Contatto IGNORATO (dati già presenti): %s", contact.email)
            return SyncResult(
                status=SyncStatus.IGNORED,
                email=contact.email,
                hubspot_contact_id=contact_id,
            )

        resp = self._request(
            "PATCH", f"/crm/v3/objects/contacts/{contact_id}", json={"properties": props}
        )

        if resp.status_code == 200:
            logger.info(
                "Contatto AGGIORNATO in HubSpot: %s (ID: %s) — campi: %s",
                contact.email,
                contact_id,
                list(props.keys()),
            )
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=contact.email,
                hubspot_contact_id=contact_id,
            )
        else:
            logger.error(
                "Errore aggiornamento HubSpot: %s %s", resp.status_code, resp.text
            )
            return SyncResult(
                status=SyncStatus.ERROR,
                email=contact.email,
                hubspot_contact_id=contact_id,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

    # ---------------------------------------------------------------- notes

    def _add_note(self, contact_id: str, email_addr: str):
        """
        Create a HubSpot engagement note tagging the contact as 'Inbound Gmail'.
        This is visible in the contact's activity timeline.
        """
        payload = {
            "properties": {
                "hs_note_body": (
                    f"📧 Email ricevuta da {email_addr} tramite Gmail.\n"
                    f"Tag: {self.config.contact_tag}\n"
                    f"Fonte: {self.config.contact_source}"
                ),
                "hs_timestamp": str(int(time.time() * 1000)),
            },
            "associations": [
                {
                    "to": {"id": contact_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 202,  # note → contact
                        }
                    ],
                }
            ],
        }
        resp = self._request("POST", "/crm/v3/objects/notes", json=payload)
        if resp.status_code in (200, 201):
            logger.debug("Nota timeline aggiunta per contatto %s", contact_id)
        else:
            logger.warning(
                "Impossibile aggiungere nota timeline: %s", resp.text[:200]
            )

    # ---------------------------------------------------------------- main entry

    def sync_contact(self, contact: ContactInfo) -> SyncResult:
        """
        Main entry point: search for existing contact, create or update.

        Returns a SyncResult with status CREATED / UPDATED / IGNORED / ERROR.
        """
        existing = self.find_contact_by_email(contact.email)

        if existing is None:
            return self.create_contact(contact)
        else:
            return self.update_contact(contact, existing)
