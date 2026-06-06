"""
Core sync loop — processes each new Gmail message and upserts it into HubSpot.

Statuses per message:
  CREATED  — new contact created in HubSpot
  UPDATED  — existing contact updated with missing fields
  SKIPPED  — already processed or invalid sender
  ERROR    — unexpected failure (logged and skipped)
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from .gmail_client import (
    build_service,
    fetch_new_messages,
    company_from_domain,
    get_current_history_id,
)
from .hubspot_client import (
    build_client,
    find_contact_by_email,
    create_contact,
    update_contact,
)
from .state import SyncState

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    status: str          # CREATED | UPDATED | SKIPPED | ERROR
    email: str
    contact_id: Optional[str] = None
    message_id: Optional[str] = None
    detail: str = ""


@dataclass
class SyncConfig:
    google_client_id: str
    google_client_secret: str
    google_refresh_token: str
    hubspot_access_token: str
    skip_domains: set = field(default_factory=set)
    state_path: str = "sync_state.json"


def run_once(config: SyncConfig) -> list[SyncResult]:
    """
    Perform one sync pass — check Gmail for new messages and upsert to HubSpot.
    Returns a list of SyncResult for each message processed this pass.
    """
    gmail = build_service(
        config.google_client_id,
        config.google_client_secret,
        config.google_refresh_token,
    )
    hs = build_client(config.hubspot_access_token)
    state = SyncState(config.state_path)

    results: list[SyncResult] = []

    for msg_id, sender in fetch_new_messages(gmail, state.history_id, config.skip_domains):
        email = sender.get("email", "")
        if not email:
            results.append(SyncResult("SKIPPED", "", message_id=msg_id, detail="no email"))
            continue

        if state.is_processed(msg_id):
            results.append(SyncResult("SKIPPED", email, message_id=msg_id, detail="already processed"))
            continue

        company_name = company_from_domain(sender["domain"], config.skip_domains)

        try:
            result = _upsert_contact(hs, sender, company_name, email, msg_id)
        except Exception as exc:
            logger.exception("Unexpected error processing message %s", msg_id)
            result = SyncResult("ERROR", email, message_id=msg_id, detail=str(exc))

        results.append(result)
        state.mark_processed(msg_id)

    # Advance the historyId so the next run only sees truly new messages
    new_history_id = get_current_history_id(gmail)
    if new_history_id:
        state.history_id = new_history_id

    state.save()
    return results


def _upsert_contact(hs, sender, company_name, email, msg_id) -> SyncResult:
    existing = find_contact_by_email(hs, email)

    if existing is None:
        contact_id = create_contact(hs, sender, company_name)
        if contact_id:
            return SyncResult("CREATED", email, contact_id=contact_id, message_id=msg_id)
        return SyncResult("ERROR", email, message_id=msg_id, detail="create failed")

    contact_id = update_contact(hs, existing.id, sender, company_name)
    if contact_id:
        return SyncResult("UPDATED", email, contact_id=contact_id, message_id=msg_id)
    return SyncResult("ERROR", email, message_id=msg_id, detail="update failed")
