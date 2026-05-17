"""Core sync logic: Gmail → HubSpot contact upsert."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .parser import ContactInfo, extract_contact, is_automated
from .hubspot_client import (
    get_client,
    find_contact_by_email,
    create_contact,
    update_contact,
)


class SyncStatus(str, Enum):
    CREATED = "Creato"
    UPDATED = "Aggiornato"
    IGNORED = "Ignorato"


@dataclass
class SyncResult:
    status: SyncStatus
    email: str
    contact_id: Optional[str]
    reason: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}] {self.email}"]
        if self.contact_id:
            parts.append(f"ID: {self.contact_id}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " | ".join(parts)


def sync_message(message: dict, hs_client=None) -> SyncResult:
    """
    Process a single Gmail message dict and sync the sender to HubSpot.

    message keys: from, subject, threadId, body, labelIds
    """
    sender = message.get("from", "")
    subject = message.get("subject", "")
    thread_id = message.get("threadId", "")
    body = message.get("body", "")

    contact = extract_contact(sender, subject, thread_id, body)

    if contact is None:
        return SyncResult(
            status=SyncStatus.IGNORED,
            email=sender,
            contact_id=None,
            reason="automated or unparseable sender",
        )

    if hs_client is None:
        hs_client = get_client()

    existing = find_contact_by_email(hs_client, contact.email)

    if existing is None:
        created = create_contact(hs_client, contact)
        return SyncResult(
            status=SyncStatus.CREATED,
            email=contact.email,
            contact_id=created["id"],
        )

    updated = update_contact(hs_client, existing["id"], contact, existing)
    if updated is existing:
        return SyncResult(
            status=SyncStatus.IGNORED,
            email=contact.email,
            contact_id=existing["id"],
            reason="already up to date",
        )
    return SyncResult(
        status=SyncStatus.UPDATED,
        email=contact.email,
        contact_id=updated["id"],
    )


def sync_messages(messages: list[dict], hs_client=None) -> list[SyncResult]:
    """Process a batch of messages, deduplicating by email."""
    if hs_client is None:
        hs_client = get_client()

    seen_emails: set[str] = set()
    results: list[SyncResult] = []

    for msg in messages:
        result = sync_message(msg, hs_client)
        # Only process each unique email once per batch
        if result.email in seen_emails and result.status == SyncStatus.CREATED:
            result.status = SyncStatus.IGNORED
            result.reason = "duplicate in batch"
        seen_emails.add(result.email)
        results.append(result)

    return results
