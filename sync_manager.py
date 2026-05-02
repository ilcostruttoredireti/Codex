from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Optional

from contact_extractor import ContactInfo, extract_contact
from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Result
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SyncResult:
    status: str          # "created" | "updated" | "ignored" | "error"
    email: str
    contact_id: Optional[str] = None

    def __str__(self) -> str:
        cid = self.contact_id or "N/A"
        return f"[{self.status.upper():8s}] {self.email:<40s} HubSpot ID: {cid}"


# ──────────────────────────────────────────────────────────────────────────────
# Automated-sender filter
# ──────────────────────────────────────────────────────────────────────────────

_IGNORE_LOCAL_PREFIXES = (
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "notifications", "bounces", "postmaster", "support",
    "newsletter", "info",
)

_IGNORE_DOMAINS = {
    "noreply.com", "no-reply.com", "mailchimp.com", "sendgrid.net",
    "bounce.com", "amazonses.com", "mailgun.org",
}


def _is_automated(email: str) -> bool:
    local, domain = email.split("@", 1)
    return (
        any(local.startswith(p) for p in _IGNORE_LOCAL_PREFIXES)
        or domain in _IGNORE_DOMAINS
    )


# ──────────────────────────────────────────────────────────────────────────────
# Sync manager
# ──────────────────────────────────────────────────────────────────────────────

class SyncManager:
    def __init__(self, gmail: GmailClient, hubspot: HubSpotClient) -> None:
        self._gmail = gmail
        self._hubspot = hubspot

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def process_message(self, message_id: str) -> SyncResult:
        message = self._gmail.get_message_headers(message_id)
        if not message:
            return SyncResult("error", "unknown")

        headers = {
            h["name"]: h["value"]
            for h in message.get("payload", {}).get("headers", [])
        }
        from_header = headers.get("From", "")
        subject = headers.get("Subject", "(no subject)")
        date_str = headers.get("Date", "")

        contact = extract_contact(from_header)
        if not contact:
            logger.debug("Could not parse contact from header: %r", from_header)
            return SyncResult("ignored", from_header or "unknown")

        if _is_automated(contact.email):
            logger.info("Skipping automated sender: %s", contact.email)
            return SyncResult("ignored", contact.email)

        timestamp_ms = _date_to_ms(date_str)
        return self._sync_contact(contact, subject, timestamp_ms)

    # ------------------------------------------------------------------
    # Core sync
    # ------------------------------------------------------------------

    def _sync_contact(
        self, contact: ContactInfo, subject: str, timestamp_ms: str
    ) -> SyncResult:
        existing = self._hubspot.find_contact_by_email(contact.email)

        if existing:
            contact_id: str = existing["id"]
            updates = _build_updates(contact, existing.get("properties", {}))
            if updates:
                self._hubspot.update_contact(contact_id, updates)
                logger.info("Updated  %s (ID %s)", contact.email, contact_id)
                status = "updated"
            else:
                logger.debug("No changes for %s (ID %s)", contact.email, contact_id)
                status = "ignored"
        else:
            result = self._hubspot.create_contact(_build_create_props(contact))
            contact_id = result["id"]
            logger.info("Created  %s (ID %s)", contact.email, contact_id)
            status = "created"

        self._hubspot.add_email_note(contact_id, contact.email, subject, timestamp_ms)

        return SyncResult(status, contact.email, contact_id)


# ──────────────────────────────────────────────────────────────────────────────
# Property builders
# ──────────────────────────────────────────────────────────────────────────────

def _build_create_props(contact: ContactInfo) -> dict[str, str]:
    props: dict[str, str] = {
        "email": contact.email,
        "lifecyclestage": "lead",
    }
    if contact.first_name:
        props["firstname"] = contact.first_name
    if contact.last_name:
        props["lastname"] = contact.last_name
    if contact.company:
        props["company"] = contact.company
    return props


def _build_updates(
    contact: ContactInfo, existing_props: dict
) -> dict[str, str]:
    """Return only the properties that are missing in the existing contact."""
    updates: dict[str, str] = {}
    if contact.first_name and not existing_props.get("firstname"):
        updates["firstname"] = contact.first_name
    if contact.last_name and not existing_props.get("lastname"):
        updates["lastname"] = contact.last_name
    if contact.company and not existing_props.get("company"):
        updates["company"] = contact.company
    return updates


# ──────────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────────

def _date_to_ms(date_str: str) -> str:
    """Convert a RFC 2822 date string to milliseconds-since-epoch string."""
    try:
        dt = parsedate_to_datetime(date_str)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return str(int(time.time() * 1000))
