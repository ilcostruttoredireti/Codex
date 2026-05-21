import logging
from dataclasses import dataclass
from typing import Literal

from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

Status = Literal["Creato", "Aggiornato", "Ignorato", "Errore"]

SOURCE_LABEL = "Gmail"


@dataclass
class SyncResult:
    status: Status
    email: str
    hubspot_id: str
    detail: str = ""

    def __str__(self) -> str:
        return f"Stato: {self.status} | Email: {self.email} | HubSpot ID: {self.hubspot_id}"


def sync_sender_to_hubspot(msg: dict, hubspot: HubSpotClient) -> SyncResult:
    """
    Process one parsed Gmail message dict and create/update the HubSpot contact.

    msg keys: id, email, name, subject, date
    """
    email = msg["email"]
    name = msg.get("name", "")
    subject = msg.get("subject", "")
    date = msg.get("date", "")

    firstname, lastname = hubspot.split_name(name) if name else ("", "")
    company = hubspot.company_from_domain(email)

    try:
        existing = hubspot.find_contact_by_email(email)
    except Exception as exc:
        logger.error("Errore ricerca HubSpot per %s: %s", email, exc)
        return SyncResult("Errore", email, "N/A", str(exc))

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})
        updates = _missing_fields(existing_props, firstname, lastname, company)

        if updates:
            try:
                hubspot.update_contact(contact_id, updates)
                logger.debug("Contatto aggiornato %s: %s", contact_id, updates)
            except Exception as exc:
                logger.error("Errore aggiornamento contatto %s: %s", contact_id, exc)
                return SyncResult("Errore", email, contact_id, str(exc))
            status: Status = "Aggiornato"
        else:
            status = "Ignorato"

        _try_add_note(hubspot, contact_id, subject, date, email)
        return SyncResult(status, email, contact_id)

    # ---- Create new contact ----
    properties: dict = {"email": email, "lead_source_detail": SOURCE_LABEL}
    if firstname:
        properties["firstname"] = firstname
    if lastname:
        properties["lastname"] = lastname
    if company:
        properties["company"] = company

    try:
        created = hubspot.create_contact(properties)
    except Exception as exc:
        # 409 = contact already exists (race condition)
        if hasattr(exc, "response") and exc.response is not None and exc.response.status_code == 409:
            logger.warning("Conflitto 409 per %s — riprovo con lookup.", email)
            existing = hubspot.find_contact_by_email(email)
            if existing:
                return sync_sender_to_hubspot(msg, hubspot)
        logger.error("Errore creazione contatto %s: %s", email, exc)
        return SyncResult("Errore", email, "N/A", str(exc))

    contact_id = created["id"]
    _try_add_note(hubspot, contact_id, subject, date, email)
    return SyncResult("Creato", email, contact_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _missing_fields(existing_props: dict, firstname: str, lastname: str, company: str) -> dict:
    updates: dict = {}
    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company
    return updates


def _try_add_note(hubspot: HubSpotClient, contact_id: str, subject: str, date: str, email: str):
    try:
        hubspot.add_email_note(contact_id, subject, date, email)
    except Exception as exc:
        logger.warning("Nota non aggiunta per contatto %s: %s", contact_id, exc)
