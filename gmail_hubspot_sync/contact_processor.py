"""
Core logic for extracting contact data from Gmail messages
and syncing them to HubSpot (create / update / skip).
"""

import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional

from gmail_client import GmailClient
from hubspot_client import HubSpotClient

logger = logging.getLogger(__name__)

# Free-mailbox providers whose domain is NOT a company name
_FREE_PROVIDERS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.it",
        "yahoo.co.uk",
        "hotmail.com",
        "hotmail.it",
        "outlook.com",
        "outlook.it",
        "live.com",
        "live.it",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "libero.it",
        "virgilio.it",
        "tin.it",
        "alice.it",
        "tiscali.it",
        "fastwebnet.it",
        "aol.com",
        "msn.com",
    ]
)

# Addresses that should always be skipped
_SKIP_PATTERNS = re.compile(
    r"^(noreply|no-reply|donotreply|do-not-reply|mailer-daemon|postmaster|"
    r"bounce|bounces|notifications?|newsletter|unsubscribe|support|info|"
    r"admin|webmaster|automated|autoresponder)[@+]",
    re.IGNORECASE,
)

StatusType = Literal["CREATO", "AGGIORNATO", "IGNORATO"]


@dataclass
class ProcessResult:
    status: StatusType
    email: str
    hubspot_id: Optional[str]
    reason: Optional[str] = None

    def __str__(self) -> str:
        id_str = f"HubSpot ID: {self.hubspot_id}" if self.hubspot_id else "–"
        reason_str = f" ({self.reason})" if self.reason else ""
        return f"[{self.status}] {self.email} → {id_str}{reason_str}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def process_message(
    message_id: str,
    gmail: GmailClient,
    hubspot: HubSpotClient,
    add_note: bool = True,
) -> ProcessResult:
    """
    Fetch one Gmail message, extract sender, sync to HubSpot.
    Returns a ProcessResult describing what happened.
    """
    message = gmail.get_message(message_id)
    sender = gmail.extract_sender_info(message)

    email_addr = sender["email"]

    # --- Skip checks ---------------------------------------------------------
    if not email_addr or "@" not in email_addr:
        return ProcessResult("IGNORATO", email_addr or "(vuoto)", None, "indirizzo non valido")

    if _SKIP_PATTERNS.match(email_addr):
        return ProcessResult("IGNORATO", email_addr, None, "indirizzo automatico")

    # --- Parse name parts ----------------------------------------------------
    firstname, lastname = _split_name(sender["name"])
    domain = email_addr.split("@", 1)[1]
    company = _company_from_domain(domain)

    # --- HubSpot sync --------------------------------------------------------
    existing = hubspot.find_contact_by_email(email_addr)

    if existing:
        contact_id = existing["id"]
        updates = _build_update_properties(existing["properties"], firstname, lastname, company)
        if updates:
            hubspot.update_contact(contact_id, updates)
            logger.debug("Aggiornati campi %s per %s", list(updates.keys()), email_addr)

        if add_note:
            hubspot.add_email_received_note(
                contact_id, email_addr, sender["subject"], sender["date"]
            )

        status: StatusType = "AGGIORNATO" if updates else "IGNORATO"
        reason = None if updates else "nessun campo da aggiornare"
        return ProcessResult(status, email_addr, contact_id, reason)

    else:
        properties = _build_create_properties(email_addr, firstname, lastname, company)
        contact_id = hubspot.create_contact(properties)

        if add_note:
            hubspot.add_email_received_note(
                contact_id, email_addr, sender["subject"], sender["date"]
            )

        return ProcessResult("CREATO", email_addr, contact_id)


# ---------------------------------------------------------------------------
# Property builders
# ---------------------------------------------------------------------------


def _build_create_properties(
    email: str,
    firstname: str,
    lastname: str,
    company: Optional[str],
) -> dict:
    props: dict = {
        "email": email,
        "hs_lead_source": "Gmail",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def _build_update_properties(
    existing_props: dict,
    firstname: str,
    lastname: str,
    company: Optional[str],
) -> dict:
    """Return only the properties that are currently blank and have a new value."""
    updates: dict = {}

    if firstname and not existing_props.get("firstname"):
        updates["firstname"] = firstname
    if lastname and not existing_props.get("lastname"):
        updates["lastname"] = lastname
    if company and not existing_props.get("company"):
        updates["company"] = company
    if not existing_props.get("hs_lead_source"):
        updates["hs_lead_source"] = "Gmail"

    return updates


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'Mario Rossi' into ('Mario', 'Rossi'). Handles edge cases."""
    full_name = full_name.strip()
    if not full_name:
        return "", ""

    parts = full_name.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _company_from_domain(domain: str) -> Optional[str]:
    """
    Infer a company name from the email domain.
    Returns None for free mailbox providers or unrecognisable domains.
    """
    domain = domain.lower().strip()

    if domain in _FREE_PROVIDERS:
        return None

    # Strip subdomains: mail.company.com → company.com
    parts = domain.split(".")
    # Keep second-level domain, ignore TLD(s)
    if len(parts) >= 2:
        company_part = parts[-2]
    else:
        company_part = parts[0]

    # Capitalise and clean up
    company_name = company_part.replace("-", " ").replace("_", " ").title()
    return company_name if company_name else None
