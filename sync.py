"""Core Gmail → HubSpot synchronisation logic."""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    message_id: str


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain.

    Strips known free-mail providers and returns an empty string for them.
    Otherwise, returns the SLD capitalised (e.g. "acme.co.uk" → "Acme").
    """
    _FREE_MAIL = {
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it",
        "hotmail.com", "hotmail.it", "outlook.com", "outlook.it",
        "live.com", "live.it", "icloud.com", "me.com", "mac.com",
        "protonmail.com", "proton.me", "libero.it", "tiscali.it",
        "virgilio.it", "alice.it", "tin.it",
    }
    if not domain or domain.lower() in _FREE_MAIL:
        return ""

    # Remove leading "www." or "mail." sub-domains
    cleaned = re.sub(r"^(www|mail|smtp|m)\.", "", domain.lower())
    # Take the second-level domain label (leftmost before TLD)
    parts = cleaned.split(".")
    sld = parts[0] if parts else cleaned
    return sld.capitalize()


def _build_note_body(sender: dict) -> str:
    lines = [
        "📧 Email ricevuta via Gmail",
        f"Da: {sender['name'] or sender['email']}",
        f"Oggetto: {sender['subject'] or '(nessun oggetto)'}",
        f"Data: {sender['date']}",
        "",
        "Tag: Inbound Gmail",
    ]
    return "\n".join(lines)


def process_message(
    message: dict,
    gmail: GmailClient,
    hubspot: HubSpotClient,
) -> SyncResult:
    """Process a single Gmail message and sync the sender to HubSpot.

    Returns a SyncResult with status CREATED, UPDATED, or IGNORED.
    """
    sender = gmail.parse_sender(message)
    email = sender["email"]

    if not email or "@" not in email:
        logger.debug(f"Skipping message {message['id']}: invalid sender email")
        return SyncResult(
            status=SyncStatus.IGNORED,
            email=email or "(sconosciuto)",
            contact_id=None,
            message_id=message["id"],
        )

    company = _company_from_domain(sender["domain"])
    existing = hubspot.find_contact_by_email(email)

    if existing:
        contact_id = existing["id"]
        existing_props = existing.get("properties", {})

        updates: dict[str, str] = {}
        if sender["first_name"] and not existing_props.get("firstname"):
            updates["firstname"] = sender["first_name"]
        if sender["last_name"] and not existing_props.get("lastname"):
            updates["lastname"] = sender["last_name"]
        if company and not existing_props.get("company"):
            updates["company"] = company
        if not existing_props.get("hs_lead_source"):
            updates["hs_lead_source"] = "OTHER"

        if updates:
            hubspot.update_contact(contact_id, updates)
            logger.info(f"Aggiornato contatto {contact_id} ({email}): {list(updates.keys())}")

        hubspot.create_note_for_contact(contact_id, _build_note_body(sender))
        return SyncResult(
            status=SyncStatus.UPDATED,
            email=email,
            contact_id=contact_id,
            message_id=message["id"],
        )

    # Create new contact
    props: dict[str, str] = {
        "email": email,
        "hs_lead_source": "OTHER",
    }
    if sender["first_name"]:
        props["firstname"] = sender["first_name"]
    if sender["last_name"]:
        props["lastname"] = sender["last_name"]
    if company:
        props["company"] = company

    created = hubspot.create_contact(props)
    contact_id = created["id"]
    logger.info(f"Creato contatto {contact_id} ({email})")

    hubspot.create_note_for_contact(contact_id, _build_note_body(sender))
    return SyncResult(
        status=SyncStatus.CREATED,
        email=email,
        contact_id=contact_id,
        message_id=message["id"],
    )
