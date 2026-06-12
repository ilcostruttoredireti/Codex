"""
Core sync logic: iterates Gmail messages, resolves contacts, upserts to HubSpot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from contact_parser import ContactInfo, parse_sender, extract_original_sender_from_forward
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

# Domains that merely forward email — we dig into the body for the real sender
FORWARDER_DOMAINS = {"latestata.it", "redazione.it"}


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
    message: str = ""

    def __str__(self) -> str:
        parts = [f"[{self.status.value}]", self.contact_email]
        if self.hubspot_id:
            parts.append(f"ID: {self.hubspot_id}")
        if self.message:
            parts.append(f"({self.message})")
        return "  ".join(parts)


class SyncEngine:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient, dry_run: bool = False):
        self.gmail = gmail
        self.hubspot = hubspot
        self.dry_run = dry_run

    def run(self, max_messages: int = 100) -> list[SyncResult]:
        results: list[SyncResult] = []
        for msg in self.gmail.unprocessed_messages(max_results=max_messages):
            result = self._process_message(msg)
            results.append(result)
            logger.info(result)
        return results

    def _process_message(self, msg: dict) -> SyncResult:
        msg_id = msg["id"]
        from_header = self.gmail.get_header(msg, "From")
        subject = self.gmail.get_header(msg, "Subject")

        info = self._resolve_contact(from_header, msg_id, msg.get("snippet", ""))
        if info is None:
            return SyncResult(SyncStatus.SKIPPED, from_header or "(unknown)", message="non parseable")

        try:
            result = self._upsert_contact(info, subject, msg_id)
            if not self.dry_run:
                self.gmail.mark_as_processed(msg_id)
            return result
        except Exception as exc:
            logger.exception("Error syncing message %s", msg_id)
            return SyncResult(SyncStatus.ERROR, info.email, message=str(exc))

    def _resolve_contact(self, from_header: str, msg_id: str, snippet: str) -> Optional[ContactInfo]:
        """Return the ContactInfo to sync, handling forwarders."""
        info = parse_sender(from_header)
        if info is None:
            return None

        # If this is a known forwarding address, look for the real sender in the body
        domain = info.email.split("@")[-1].lower()
        if domain in FORWARDER_DOMAINS:
            full_body = self.gmail.get_message_snippet(msg_id)
            original = extract_original_sender_from_forward(full_body or snippet)
            if original:
                original.source_message_id = msg_id
                return original
            return None  # forwarded but no original sender found

        info.source_message_id = msg_id
        return info

    def _upsert_contact(self, info: ContactInfo, subject: str, msg_id: str) -> SyncResult:
        existing = self.hubspot.find_contact_by_email(info.email)

        if existing is None:
            if self.dry_run:
                return SyncResult(SyncStatus.CREATED, info.email, message="dry-run")
            created = self.hubspot.create_contact(info)
            contact_id = created.id
            self.hubspot.create_email_activity(contact_id, subject, msg_id)
            return SyncResult(SyncStatus.CREATED, info.email, hubspot_id=contact_id)
        else:
            contact_id = existing.id if hasattr(existing, "id") else existing["id"]
            if self.dry_run:
                return SyncResult(SyncStatus.UPDATED, info.email, hubspot_id=str(contact_id), message="dry-run")
            self.hubspot.update_contact(str(contact_id), info, existing)
            self.hubspot.create_email_activity(str(contact_id), subject, msg_id)
            return SyncResult(SyncStatus.UPDATED, info.email, hubspot_id=str(contact_id))
