import logging
from typing import Literal

from hubspot import HubSpot

import state
import contact_parser
import hubspot_client

log = logging.getLogger(__name__)

SyncStatus = Literal["created", "updated", "skipped", "error"]


def sync_message(hs_client: HubSpot, message: dict) -> dict:
    """
    Process a single Gmail message and synchronise the sender to HubSpot.

    Returns a result dict:
        {
            "status":     "created" | "updated" | "skipped" | "error",
            "email":      str,
            "contact_id": str | None,
            "reason":     str,          # populated on "skipped" / "error"
        }
    """
    message_id: str = message["id"]

    # Skip messages we've already processed (idempotency guard)
    if state.is_processed(message_id):
        return _result("skipped", "", None, "already_processed")

    from_header: str = message.get("from", "")
    info = contact_parser.parse_sender(from_header)
    email: str = info.get("email", "")

    if not email:
        state.mark_processed(message_id)
        return _result("skipped", "", None, "no_email_in_header")

    # ------------------------------------------------------------------
    # Check for existing contact
    # ------------------------------------------------------------------
    existing = hubspot_client.find_contact_by_email(hs_client, email)

    if existing:
        contact_id: str = existing["id"]
        updates = _missing_fields(info, existing.get("properties") or {})
        if updates:
            hubspot_client.update_contact(hs_client, contact_id, updates)
            log.debug("Updated contact %s with fields: %s", contact_id, list(updates))
        status: SyncStatus = "updated"
    else:
        created = hubspot_client.create_contact(hs_client, _create_properties(info))
        if not created:
            state.mark_processed(message_id)
            return _result("error", email, None, "hubspot_create_failed")
        contact_id = created["id"]
        status = "created"

    # ------------------------------------------------------------------
    # Add timeline activity note
    # ------------------------------------------------------------------
    hubspot_client.add_email_activity(
        hs_client,
        contact_id,
        email,
        message.get("subject", ""),
        message.get("date", ""),
    )

    state.mark_processed(message_id)
    return _result(status, email, contact_id)


# ---------------------------------------------------------------------------
# Property builders
# ---------------------------------------------------------------------------

def _create_properties(info: dict) -> dict:
    """Build the HubSpot properties dict for a brand-new contact."""
    props = {
        "email": info["email"],
        "lifecyclestage": "lead",
        "hs_lead_source": "OFFLINE",
    }
    if info.get("firstname"):
        props["firstname"] = info["firstname"]
    if info.get("lastname"):
        props["lastname"] = info["lastname"]
    if info.get("company"):
        props["company"] = info["company"]
    return props


def _missing_fields(info: dict, existing_props: dict) -> dict:
    """
    Return only the fields that are absent (None / empty) in the existing
    HubSpot contact so we never overwrite data the user entered manually.
    """
    updates: dict = {}
    for hs_key, value in [
        ("firstname", info.get("firstname")),
        ("lastname", info.get("lastname")),
        ("company", info.get("company")),
    ]:
        if value and not existing_props.get(hs_key):
            updates[hs_key] = value
    return updates


def _result(
    status: SyncStatus,
    email: str,
    contact_id,
    reason: str = "",
) -> dict:
    return {
        "status": status,
        "email": email,
        "contact_id": contact_id,
        "reason": reason,
    }
