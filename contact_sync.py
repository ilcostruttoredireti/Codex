"""
Core sync logic: for each Gmail message, decide whether to create, update,
or skip the corresponding HubSpot contact, then log the outcome.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    contact_id: Optional[str]
    reason: str = ""

    def __str__(self) -> str:
        parts = [
            f"Stato: {self.status.value}",
            f"Email: {self.contact_email}",
            f"ID HubSpot: {self.contact_id or 'N/A'}",
        ]
        if self.reason:
            parts.append(f"Nota: {self.reason}")
        return " | ".join(parts)


# ------------------------------------------------------------------
# Emails that should never be synced
# ------------------------------------------------------------------

_SKIP_ADDRESSES = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "bounce", "mailer-daemon", "postmaster",
}


def _should_skip(email: str) -> bool:
    local = email.split("@")[0].lower().replace(".", "").replace("-", "").replace("_", "")
    return any(skip in local for skip in _SKIP_ADDRESSES)


# ------------------------------------------------------------------
# Main sync function
# ------------------------------------------------------------------

def sync_message(message: dict, gmail: GmailClient, hubspot: HubSpotClient) -> SyncResult:
    """
    Process a single Gmail message dict and sync the sender to HubSpot.
    Returns a SyncResult describing what happened.
    """
    sender = gmail.extract_sender(message)
    email = sender.get("email", "")

    if not email or "@" not in email:
        return SyncResult(SyncStatus.SKIPPED, email or "(empty)", None, "No valid email in From: header")

    if _should_skip(email):
        return SyncResult(SyncStatus.SKIPPED, email, None, "Automated/no-reply sender")

    new_props = hubspot.build_contact_properties(sender)

    try:
        existing = hubspot.find_contact_by_email(email)

        if existing:
            contact_id = existing["id"]
            updates = hubspot.merge_properties(existing, new_props)

            if updates:
                hubspot.update_contact(contact_id, updates)
                _create_note(hubspot, contact_id, sender)
                return SyncResult(SyncStatus.UPDATED, email, contact_id,
                                  f"Updated fields: {', '.join(updates.keys())}")
            else:
                # Contact exists and no fields need updating; still log the email activity
                _create_note(hubspot, contact_id, sender)
                return SyncResult(SyncStatus.SKIPPED, email, contact_id,
                                  "Contact already up to date")

        else:
            created = hubspot.create_contact(new_props)
            contact_id = created["id"]
            _create_note(hubspot, contact_id, sender)
            return SyncResult(SyncStatus.CREATED, email, contact_id)

    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "?"
        # HubSpot returns 409 when a contact with that email already exists
        # (race condition between search and create)
        if status_code == 409:
            try:
                existing = hubspot.find_contact_by_email(email)
                if existing:
                    return SyncResult(SyncStatus.SKIPPED, email, existing["id"],
                                      "Duplicate detected and resolved")
            except Exception:
                pass
        return SyncResult(SyncStatus.ERROR, email, None, f"HTTP {status_code}: {e}")
    except Exception as e:
        logger.exception("Unexpected error syncing %s", email)
        return SyncResult(SyncStatus.ERROR, email, None, str(e))


def _create_note(hubspot: HubSpotClient, contact_id: str, sender: dict) -> None:
    hubspot.create_email_received_note(
        contact_id=contact_id,
        sender_email=sender.get("email", ""),
        subject=sender.get("subject", ""),
        date=sender.get("date", ""),
    )
