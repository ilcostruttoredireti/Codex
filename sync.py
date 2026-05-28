"""
Core sync logic: Gmail messages → HubSpot contacts.

This module is transport-agnostic: it accepts pre-parsed message dicts
(as returned by GmailClient) and returns SyncResult objects, making it
straightforward to test without live API calls.
"""
from __future__ import annotations

import logging
from typing import Iterator, TYPE_CHECKING

from models import SenderContact, SyncResult, SyncStatus
from parser import parse_from_header

if TYPE_CHECKING:
    from gmail_client import GmailClient
    from hubspot_client import HubSpotClient

log = logging.getLogger(__name__)

# Domains we never sync (internal, noreply, mailing lists, etc.)
_SKIP_DOMAINS = frozenset({
    "noreply.com", "no-reply.com", "mailer-daemon.com",
    "bounce.com", "notifications.google.com",
})
_SKIP_PREFIXES = ("noreply@", "no-reply@", "mailer-daemon@", "bounce@", "notifications@")


def _should_skip(contact: SenderContact) -> bool:
    if not contact.email or "@" not in contact.email:
        return True
    if contact.domain in _SKIP_DOMAINS:
        return True
    for prefix in _SKIP_PREFIXES:
        if contact.email.startswith(prefix):
            return True
    return False


def sync_message(
    message: dict,
    hs: HubSpotClient,
) -> SyncResult:
    """
    Process a single Gmail message dict and upsert the sender into HubSpot.

    Returns a SyncResult describing what happened.
    """
    raw_from = message.get("from", "")
    subject = message.get("subject", "")
    msg_id = message.get("id", "")

    contact = parse_from_header(raw_from)

    if _should_skip(contact):
        log.debug("Skipping %s (no-reply / automated sender)", contact.email)
        return SyncResult(
            status=SyncStatus.SKIPPED,
            email=contact.email,
            message_id=msg_id,
            subject=subject,
        )

    try:
        existing = hs.find_contact_by_email(contact.email)

        if existing:
            hs.update_contact(existing["id"], contact, existing["properties"])
            hs.add_email_received_note(existing["id"], subject, contact.email)
            return SyncResult(
                status=SyncStatus.UPDATED,
                email=contact.email,
                hubspot_id=existing["id"],
                message_id=msg_id,
                subject=subject,
            )
        else:
            new_id = hs.create_contact(contact)
            hs.add_email_received_note(new_id, subject, contact.email)
            return SyncResult(
                status=SyncStatus.CREATED,
                email=contact.email,
                hubspot_id=new_id,
                message_id=msg_id,
                subject=subject,
            )

    except Exception as exc:
        log.error("Errore sincronizzando %s: %s", contact.email, exc)
        return SyncResult(
            status=SyncStatus.SKIPPED,
            email=contact.email,
            message_id=msg_id,
            subject=subject,
            error=str(exc),
        )


def run_sync_batch(
    gmail: "GmailClient",
    hs: "HubSpotClient",
    processed_label: str = "HubSpot-Synced",
) -> Iterator[SyncResult]:
    """
    Fetch all unprocessed inbox messages, sync them, then mark each one.

    Yields a SyncResult per message so callers can log / display progress.
    """
    for message in gmail.iter_unprocessed_messages(processed_label=processed_label):
        result = sync_message(message, hs)
        yield result
        # Mark regardless of outcome so we don't re-process
        try:
            gmail.mark_as_processed(message["id"], label_name=processed_label)
        except Exception as exc:
            log.warning("Impossibile etichettare messaggio %s: %s", message["id"], exc)
