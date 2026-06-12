"""Core sync logic — stateless, returns SyncResult for each message."""
from __future__ import annotations

import logging
from typing import Optional

from models import SenderInfo, SyncResult, SyncStatus
import hubspot_client as hs

logger = logging.getLogger(__name__)

# Well-known senders to ignore (newsletters, no-reply, notifications)
_IGNORE_PREFIXES = ("no-reply", "noreply", "mailer-daemon", "postmaster",
                    "notifications@", "newsletter", "bounce")


def should_ignore(email: str) -> bool:
    email_lower = email.lower()
    return any(email_lower.startswith(p) or p in email_lower for p in _IGNORE_PREFIXES)


def sync_sender(raw_from: str, subject: str = "", snippet: str = "",
                date: str = "", log_activity: bool = True) -> SyncResult:
    """
    Given a raw From header, sync the sender to HubSpot.
    Returns a SyncResult.
    """
    sender = SenderInfo.from_raw(raw_from)

    if not sender.email or "@" not in sender.email:
        return SyncResult(SyncStatus.IGNORED, raw_from, message="invalid email")

    if should_ignore(sender.email):
        return SyncResult(SyncStatus.IGNORED, sender.email,
                          message="no-reply / automated sender")

    existing = hs.find_contact_by_email(sender.email)

    if existing is None:
        created = hs.create_contact(
            email=sender.email,
            first_name=sender.first_name,
            last_name=sender.last_name,
            company=sender.company,
        )
        contact_id = str(created.get("id", ""))
        if log_activity and contact_id:
            hs.log_email_activity(contact_id, subject, snippet, date)
        logger.info("CREATED  %s  id=%s", sender.email, contact_id)
        return SyncResult(SyncStatus.CREATED, sender.email, contact_id)

    contact_id = str(existing.get("id", ""))
    updated = hs.update_contact(
        contact_id=contact_id,
        first_name=sender.first_name,
        last_name=sender.last_name,
        company=sender.company,
        existing=existing,
    )
    changed = updated != existing
    if log_activity and contact_id:
        hs.log_email_activity(contact_id, subject, snippet, date)

    status = SyncStatus.UPDATED if changed else SyncStatus.IGNORED
    logger.info("%s  %s  id=%s", status.value.upper(), sender.email, contact_id)
    return SyncResult(status, sender.email, contact_id)
