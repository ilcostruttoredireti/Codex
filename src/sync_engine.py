import json
import logging
import os
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .contact_extractor import extract_contact_from_sender
from .gmail_client import GmailClient
from .hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

STATE_FILE = "state.json"


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    SKIPPED = "Ignorato"
    ERROR = "Errore"


@dataclass
class SyncResult:
    status: SyncStatus
    contact_email: str
    hubspot_id: Optional[str] = None
    reason: Optional[str] = None

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.contact_email}"]
        if self.hubspot_id:
            parts.append(f"HubSpot ID: {self.hubspot_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


# ------------------------------------------------------------------
# State persistence
# ------------------------------------------------------------------

def _load_state() -> dict:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        logger.error("Cannot save state: %s", e)


# ------------------------------------------------------------------
# Single-message processing
# ------------------------------------------------------------------

def process_message(
    message: dict,
    gmail: GmailClient,
    hubspot: HubSpotClient,
    create_activity: bool = True,
) -> SyncResult:
    msg_id = message.get("id", "?")
    headers = gmail.get_message_headers(msg_id)

    if not headers:
        return SyncResult(SyncStatus.ERROR, f"msg:{msg_id}", reason="Impossibile leggere headers")

    from_header = headers["from"]
    subject = headers["subject"]

    contact = extract_contact_from_sender(from_header)
    if not contact:
        return SyncResult(SyncStatus.SKIPPED, from_header, reason="Mittente non valido o automatico")

    existing = hubspot.find_contact_by_email(contact.email)

    if existing:
        contact_id = existing.id
        hubspot.update_contact_if_needed(contact_id, contact, existing)
        if create_activity:
            hubspot.add_email_activity(contact_id, subject, contact.email)
        return SyncResult(SyncStatus.UPDATED, contact.email, hubspot_id=contact_id)
    else:
        contact_id = hubspot.create_contact(contact)
        if not contact_id:
            return SyncResult(SyncStatus.ERROR, contact.email, reason="Creazione HubSpot fallita")
        if create_activity:
            hubspot.add_email_activity(contact_id, subject, contact.email)
        return SyncResult(SyncStatus.CREATED, contact.email, hubspot_id=contact_id)


# ------------------------------------------------------------------
# Monitoring loop
# ------------------------------------------------------------------

def run_sync_loop(
    gmail: GmailClient,
    hubspot_client: HubSpotClient,
    poll_interval: int = 60,
    create_activity: bool = True,
) -> None:
    state = _load_state()
    history_id = state.get("last_history_id")

    if not history_id:
        history_id = gmail.get_current_history_id()
        state["last_history_id"] = history_id
        _save_state(state)
        logger.info("Initialized. Starting history ID: %s", history_id)
        logger.info("Monitoring started — waiting for new emails...")

    while True:
        try:
            messages = gmail.get_new_messages_since(history_id)

            if not messages:
                logger.debug("No new messages. Next check in %ds.", poll_interval)
            else:
                logger.info("Found %d new message(s) to process.", len(messages))

                seen_emails: set[str] = set()
                for message in messages:
                    result = process_message(message, gmail, hubspot_client, create_activity)

                    # Deduplicate within the same batch (multiple emails from same sender)
                    if result.contact_email in seen_emails and result.status == SyncStatus.CREATED:
                        result = SyncResult(
                            SyncStatus.UPDATED, result.contact_email,
                            hubspot_id=result.hubspot_id, reason="Duplicato nel batch"
                        )
                    seen_emails.add(result.contact_email)

                    print(result)
                    logger.info(str(result))

            # Advance history ID to current position
            new_history_id = gmail.get_current_history_id()
            if new_history_id != history_id:
                history_id = new_history_id
                state["last_history_id"] = history_id
                _save_state(state)

        except KeyboardInterrupt:
            logger.info("Stopping monitor (KeyboardInterrupt).")
            break
        except Exception as e:
            logger.error("Unexpected error in sync loop: %s", e, exc_info=True)

        time.sleep(poll_interval)
