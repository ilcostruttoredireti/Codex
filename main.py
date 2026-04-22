"""
Gmail → HubSpot contact sync
=============================
Polls Gmail for new inbox messages, extracts sender info, and creates or
updates the corresponding contact in HubSpot.

Run:
    python main.py

First run: an OAuth2 browser window opens for Gmail authentication.
Subsequent runs reuse the cached token stored in GMAIL_TOKEN_FILE.
"""

import logging
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import config  # noqa: F401 — triggers env-var validation and logging setup
from config import (
    GMAIL_CREDENTIALS_FILE,
    GMAIL_TOKEN_FILE,
    HUBSPOT_API_KEY,
    HUBSPOT_INBOUND_LIST_ID,
    POLL_INTERVAL_SECONDS,
    STATE_FILE,
)
from contact_extractor import ContactInfo, extract_contact_from_sender
from gmail_client import GmailClient
from hubspot_client import HubSpotClient
from state_manager import StateManager

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    hubspot_id: Optional[str]
    message_id: str


# ---------------------------------------------------------------------------
# HubSpot property builders
# ---------------------------------------------------------------------------


def _props_for_new_contact(contact: ContactInfo) -> dict:
    props: dict = {"email": contact.email}
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    return props


def _props_for_update(contact: ContactInfo, existing: dict) -> dict:
    """Return only the fields that are missing in the existing record."""
    current = existing.get("properties", {})
    props: dict = {}
    if contact.first_name and not current.get("firstname"):
        props["firstname"] = contact.first_name
    if contact.last_name and not current.get("lastname"):
        props["lastname"] = contact.last_name
    if contact.company and not current.get("company"):
        props["company"] = contact.company
    return props


# ---------------------------------------------------------------------------
# Per-message processing
# ---------------------------------------------------------------------------


def process_message(
    message: dict,
    gmail: GmailClient,
    hubspot: HubSpotClient,
    state: StateManager,
) -> SyncResult:
    msg_id = message["id"]
    from_header = gmail.get_header(message, "From") or ""
    subject = gmail.get_header(message, "Subject") or "(no subject)"

    contact = extract_contact_from_sender(from_header)
    if contact is None:
        state.mark_processed(msg_id)
        return SyncResult(
            status=SyncStatus.IGNORED,
            email=from_header,
            hubspot_id=None,
            message_id=msg_id,
        )

    existing = hubspot.find_contact_by_email(contact.email)

    if existing:
        contact_id: str = existing["id"]
        update_props = _props_for_update(contact, existing)
        if update_props:
            hubspot.update_contact(contact_id, update_props)
            status = SyncStatus.UPDATED
        else:
            status = SyncStatus.IGNORED
    else:
        created = hubspot.create_contact(_props_for_new_contact(contact))
        contact_id = created["id"]
        status = SyncStatus.CREATED

    if status in (SyncStatus.CREATED, SyncStatus.UPDATED):
        note_body = (
            f"Email ricevuta via Gmail\n"
            f"Fonte contatto: Inbound Gmail\n"
            f"Oggetto: {subject}\n"
            f"Mittente: {contact.email}"
        )
        hubspot.add_note(contact_id, note_body)
        hubspot.add_to_inbound_list(contact_id)

    state.mark_processed(msg_id)
    return SyncResult(
        status=status,
        email=contact.email,
        hubspot_id=contact_id,
        message_id=msg_id,
    )


# ---------------------------------------------------------------------------
# Polling loop
# ---------------------------------------------------------------------------


def run_sync_loop(gmail: GmailClient, hubspot: HubSpotClient, state: StateManager):
    logger.info(f"Sync loop started — polling every {POLL_INTERVAL_SECONDS}s")

    while True:
        try:
            history_id = state.get_history_id()
            messages, new_history_id = gmail.fetch_new_messages(history_id)
            state.set_history_id(new_history_id)

            new_messages = [m for m in messages if not state.is_processed(m["id"])]

            if new_messages:
                logger.info(f"{len(new_messages)} new message(s) to process")

            for message in new_messages:
                try:
                    result = process_message(message, gmail, hubspot, state)
                    logger.info(
                        f"[{result.status.value}] "
                        f"email={result.email} | "
                        f"hubspot_id={result.hubspot_id or '-'} | "
                        f"msg_id={result.message_id}"
                    )
                except Exception as exc:
                    logger.error(f"Error on message {message['id']}: {exc}", exc_info=True)

        except KeyboardInterrupt:
            raise
        except Exception as exc:
            logger.error(f"Sync cycle error: {exc}", exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    gmail = GmailClient(GMAIL_CREDENTIALS_FILE, GMAIL_TOKEN_FILE)
    gmail.connect()

    hubspot = HubSpotClient(HUBSPOT_API_KEY, inbound_list_id=HUBSPOT_INBOUND_LIST_ID)
    state = StateManager(STATE_FILE)

    try:
        run_sync_loop(gmail, hubspot, state)
    except KeyboardInterrupt:
        logger.info("Interrupted — shutting down")
        sys.exit(0)
    finally:
        hubspot.close()


if __name__ == "__main__":
    main()
