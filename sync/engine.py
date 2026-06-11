"""Core sync logic: extract contact info from email, create/update in HubSpot."""

import logging
import re
from typing import Dict, Optional, Tuple

from .config import PERSONAL_EMAIL_DOMAINS
from . import hubspot_client as hs

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _split_name(full_name: str) -> Tuple[str, str]:
    """Return (firstname, lastname) from a full display name."""
    parts = full_name.strip().split(maxsplit=1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0] if parts else "", "")


def _company_from_domain(domain: str) -> str:
    """
    Derive a readable company name from an email domain.
    Returns empty string for personal/free email providers.
    """
    domain = domain.lower()
    if not domain or domain in PERSONAL_EMAIL_DOMAINS:
        return ""

    # Strip www. prefix
    domain = re.sub(r"^www\.", "", domain)

    # Take the first label of the domain (e.g. "acme" from "acme.co.uk")
    company_label = domain.split(".")[0]

    # Humanise: replace hyphens/underscores, title-case
    return company_label.replace("-", " ").replace("_", " ").title()


def _build_props(msg_info: Dict) -> Dict[str, str]:
    """Build HubSpot contact properties dict from parsed email info."""
    display_name = msg_info.get("display_name", "")
    email_addr: str = msg_info["email"]
    domain: str = msg_info.get("domain", "")

    firstname, lastname = _split_name(display_name) if display_name else ("", "")

    # Fall back: derive firstname from the email local-part
    if not firstname:
        local_part = email_addr.split("@")[0]
        firstname = local_part.replace(".", " ").replace("_", " ").title()

    company = _company_from_domain(domain)

    props: Dict[str, str] = {"email": email_addr, "firstname": firstname}
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company

    return props


def _build_note(msg_info: Dict, action: str) -> str:
    """Build the HubSpot note body for this Gmail event."""
    lines = [
        f"📧 Fonte: Gmail  |  Tag: Inbound Gmail",
        f"Azione: {action}",
        f"Mittente: {msg_info['email']}",
    ]
    subject = msg_info.get("subject", "").strip()
    if subject:
        lines.append(f"Oggetto: {subject}")
    date_h = msg_info.get("date_header", "").strip()
    if date_h:
        lines.append(f"Data email: {date_h}")
    return "\n".join(lines)


# ------------------------------------------------------------------ #
# Main sync function                                                  #
# ------------------------------------------------------------------ #

SyncResult = Dict  # {'status', 'email', 'contact_id'}


def sync_contact(msg_info: Dict) -> SyncResult:
    """
    Sync one email sender to HubSpot.

    Returns a dict with keys:
        status     : 'created' | 'updated' | 'skipped' | 'error'
        email      : sender email address
        contact_id : HubSpot contact ID string, or None on error
    """
    email_addr: str = msg_info["email"]
    base_result: SyncResult = {"email": email_addr, "contact_id": None}

    existing = hs.search_contact_by_email(email_addr)
    new_props = _build_props(msg_info)

    if existing:
        contact_id: str = existing["id"]
        existing_props: Dict = existing.get("properties", {})

        # Only fill in fields that are blank in HubSpot
        updates = {
            k: v
            for k, v in new_props.items()
            if k != "email" and v and not existing_props.get(k)
        }

        if updates:
            hs.update_contact(contact_id, updates)
            status = "updated"
        else:
            status = "skipped"

        hs.create_note(
            contact_id,
            _build_note(msg_info, "Email ricevuta – contatto già esistente"),
            msg_info.get("internal_date_ms", 0),
        )

        return {**base_result, "status": status, "contact_id": contact_id}

    else:
        created = hs.create_contact(new_props)
        if not created:
            return {**base_result, "status": "error"}

        contact_id = created["id"]
        hs.create_note(
            contact_id,
            _build_note(msg_info, "Nuovo contatto creato automaticamente da Gmail"),
            msg_info.get("internal_date_ms", 0),
        )
        return {**base_result, "status": "created", "contact_id": contact_id}
