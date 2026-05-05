import logging
from dataclasses import dataclass, field
from typing import Literal

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

Status = Literal["Creato", "Aggiornato", "Ignorato", "Errore"]


@dataclass
class SyncResult:
    email: str
    status: Status
    contact_id: str | None = field(default=None)

    def __str__(self) -> str:
        return (
            f"Stato: {self.status:<12} | "
            f"Email: {self.email:<40} | "
            f"ID HubSpot: {self.contact_id or '—'}"
        )


def process_email(
    hubspot: HubSpotClient,
    email_data: dict,
) -> SyncResult:
    """Sync a single sender to HubSpot and return the outcome."""
    email_addr = email_data["email"]
    existing = hubspot.find_contact_by_email(email_addr)

    if existing:
        updates = hubspot.build_updates(existing, email_data)
        if updates:
            hubspot.update_contact(existing.id, updates)
            hubspot.add_email_note(existing.id, email_data)
            return SyncResult(email=email_addr, status="Aggiornato", contact_id=existing.id)
        else:
            return SyncResult(email=email_addr, status="Ignorato", contact_id=existing.id)
    else:
        contact = hubspot.create_contact(email_data)
        if contact:
            hubspot.add_email_note(contact.id, email_data)
            return SyncResult(email=email_addr, status="Creato", contact_id=contact.id)
        return SyncResult(email=email_addr, status="Errore")


def run_cycle(gmail: GmailClient, hubspot: HubSpotClient) -> list[SyncResult]:
    """Fetch new Gmail messages and sync each sender. Returns results."""
    emails = gmail.get_new_emails()
    if not emails:
        logger.debug("Nessuna nuova email da processare.")
        return []

    results: list[SyncResult] = []
    for email_data in emails:
        result = process_email(hubspot, email_data)
        results.append(result)
        logger.info(str(result))

    return results
