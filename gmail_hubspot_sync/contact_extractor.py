"""Parse email From headers into structured ContactInfo objects."""

import email.utils
from dataclasses import dataclass
from typing import Optional

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "aol.com", "live.com", "msn.com",
    "protonmail.com", "mail.com", "zoho.com", "libero.it",
    "virgilio.it", "alice.it", "tiscali.it", "fastwebnet.it",
}

IGNORED_SENDERS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "notifications", "newsletter",
    "info", "support", "hello", "contact",
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None


def extract_contact(from_header: str) -> Optional[ContactInfo]:
    """
    Parse a From header string into a ContactInfo.
    Returns None if the sender should be ignored (noreply, mailer-daemon, etc.).
    """
    display_name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()

    if not addr or "@" not in addr:
        return None

    local_part, domain = addr.split("@", 1)

    # Skip automated senders
    if any(ignored in local_part for ignored in IGNORED_SENDERS):
        return None

    # Parse display name into first / last
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    if display_name:
        name = display_name.strip().strip('"')
        parts = name.split(None, 1)
        first_name = parts[0] if parts else None
        last_name = parts[1] if len(parts) > 1 else None

    # Derive company from domain (skip personal domains)
    company: Optional[str] = None
    business_domain: Optional[str] = None
    if domain not in PERSONAL_DOMAINS:
        business_domain = domain
        # e.g. "acme.com" → "Acme", "mail.acme.co.uk" → "Acme"
        root = domain.split(".")[-2] if domain.count(".") >= 1 else domain.split(".")[0]
        company = root.title()

    return ContactInfo(
        email=addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=business_domain,
    )
