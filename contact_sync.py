"""
Core business logic: given parsed Gmail sender data, decide whether to
create, update, or ignore the corresponding HubSpot contact.
"""

import logging

from hubspot_client import (
    company_from_domain,
    create_contact,
    create_note,
    search_contact_by_email,
    split_full_name,
    update_contact,
)

logger = logging.getLogger(__name__)

# Local-part keywords that identify automated / system senders
_AUTOMATED_KEYWORDS = frozenset(
    {
        "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
        "notifications", "notification", "mailer-daemon", "postmaster",
        "bounce", "auto-reply", "autoreply", "support", "info",
        "newsletter", "alerts",
    }
)


def process_email_sender(msg_data: dict) -> dict:
    """
    Evaluate one email sender and sync the contact to HubSpot.

    Returns a result dict::

        {
            "status":     "created" | "updated" | "ignored" | "error",
            "email":      "sender@example.com",
            "hubspot_id": "123456" | None,
        }
    """
    email_addr: str = msg_data["email"]

    if _is_automated(email_addr):
        logger.debug("Ignored automated sender: %s", email_addr)
        return {"status": "ignored", "email": email_addr, "hubspot_id": None}

    first_name, last_name = split_full_name(msg_data.get("name"))
    company = company_from_domain(msg_data.get("domain"))

    try:
        existing = search_contact_by_email(email_addr)

        if existing:
            return _handle_existing(existing, email_addr, first_name, last_name, company, msg_data)

        return _handle_new(email_addr, first_name, last_name, company, msg_data)

    except Exception as exc:
        logger.error("HubSpot error for %s: %s", email_addr, exc)
        return {"status": "error", "email": email_addr, "hubspot_id": None}


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_automated(email_addr: str) -> bool:
    local = email_addr.split("@")[0].lower()
    return any(kw in local for kw in _AUTOMATED_KEYWORDS)


def _handle_existing(
    existing: dict,
    email_addr: str,
    first_name: str | None,
    last_name: str | None,
    company: str | None,
    msg_data: dict,
) -> dict:
    contact_id = existing["id"]
    props = existing.get("properties", {})

    updates: dict = {}
    if first_name and not props.get("firstname"):
        updates["firstname"] = first_name
    if last_name and not props.get("lastname"):
        updates["lastname"] = last_name
    if company and not props.get("company"):
        updates["company"] = company

    if updates:
        update_contact(contact_id, updates)
        _attach_note(contact_id, msg_data, is_new=False)
        logger.debug("Updated %s (id=%s) with %s", email_addr, contact_id, list(updates))
        return {"status": "updated", "email": email_addr, "hubspot_id": contact_id}

    # Contact exists and all available fields are already populated
    return {"status": "ignored", "email": email_addr, "hubspot_id": contact_id}


def _handle_new(
    email_addr: str,
    first_name: str | None,
    last_name: str | None,
    company: str | None,
    msg_data: dict,
) -> dict:
    properties: dict = {
        "email": email_addr,
        "lifecyclestage": "lead",
    }
    if first_name:
        properties["firstname"] = first_name
    if last_name:
        properties["lastname"] = last_name
    if company:
        properties["company"] = company

    created = create_contact(properties)
    contact_id = created["id"]

    _attach_note(contact_id, msg_data, is_new=True)
    logger.debug("Created contact %s (id=%s)", email_addr, contact_id)
    return {"status": "created", "email": email_addr, "hubspot_id": contact_id}


def _attach_note(contact_id: str, msg_data: dict, is_new: bool) -> None:
    action = "Nuovo contatto creato" if is_new else "Email ricevuta"
    body = (
        f"[Inbound Gmail] {action}\n"
        f"Oggetto: {msg_data.get('subject', 'N/A')}\n"
        f"Data: {msg_data.get('date', 'N/A')}\n"
        f"Fonte contatto: Gmail\n"
        f"Tag: Inbound Gmail"
    )
    create_note(contact_id, body, timestamp_ms=msg_data.get("internal_date_ms"))
