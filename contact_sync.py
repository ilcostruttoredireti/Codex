import logging
from typing import TypedDict, Optional

from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncResult(TypedDict):
    status: str          # "Creato" | "Aggiornato" | "Ignorato" | "Errore"
    email: str
    id: Optional[str]


class ContactSync:
    def __init__(self, hubspot_token: str, create_notes: bool = True):
        self.hs = HubSpotClient(hubspot_token)
        self.create_notes = create_notes

    def sync(self, sender: dict) -> SyncResult:
        """Sync one Gmail sender to HubSpot. Returns a SyncResult."""
        email = sender["email"]
        try:
            existing = self.hs.find_contact_by_email(email)
            if existing:
                return self._update(existing, sender)
            return self._create(sender)
        except Exception as exc:
            logger.error("Error syncing %s: %s", email, exc, exc_info=True)
            return {"status": "Errore", "email": email, "id": None}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _base_properties(self, sender: dict) -> dict:
        props: dict = {"email": sender["email"]}
        if sender.get("first_name"):
            props["firstname"] = sender["first_name"]
        if sender.get("last_name"):
            props["lastname"] = sender["last_name"]
        if sender.get("company"):
            props["company"] = sender["company"]
        return props

    def _create(self, sender: dict) -> SyncResult:
        props = self._base_properties(sender)
        # Mark lead status and inbound source
        props["hs_lead_status"] = "NEW"

        result = self.hs.create_contact(props)
        contact_id = result["id"]

        if self.create_notes:
            self.hs.create_note(
                contact_id,
                (
                    "Contatto creato automaticamente da email in entrata Gmail.\n"
                    f"Oggetto: {sender.get('subject') or 'N/A'}\n"
                    "Tag: Inbound Gmail"
                ),
            )

        logger.info("Creato  %s (ID: %s)", sender["email"], contact_id)
        return {"status": "Creato", "email": sender["email"], "id": contact_id}

    def _update(self, existing: dict, sender: dict) -> SyncResult:
        contact_id = existing["id"]
        current = existing.get("properties", {})

        # Only fill in fields that are genuinely empty
        updates: dict = {}
        for hs_field, value in [
            ("firstname", sender.get("first_name")),
            ("lastname", sender.get("last_name")),
            ("company", sender.get("company")),
        ]:
            if value and not current.get(hs_field):
                updates[hs_field] = value

        if updates:
            self.hs.update_contact(contact_id, updates)
            status = "Aggiornato"
            if self.create_notes:
                self.hs.create_note(
                    contact_id,
                    (
                        "Nuova email in entrata ricevuta via Gmail.\n"
                        f"Oggetto: {sender.get('subject') or 'N/A'}\n"
                        "Tag: Inbound Gmail"
                    ),
                )
        else:
            status = "Ignorato"

        logger.info("%s  %s (ID: %s)", status, sender["email"], contact_id)
        return {"status": status, "email": sender["email"], "id": contact_id}
