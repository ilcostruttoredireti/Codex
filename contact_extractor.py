"""Parse sender contact data from raw email headers."""

import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

_SKIP_PATTERNS = (
    "noreply", "no-reply", "donotreply", "mailer-daemon",
    "postmaster", "bounce", "notifications@", "alerts@",
    "support@", "info@", "admin@", "newsletter@",
)

_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "tutanota.com", "zoho.com", "ymail.com", "mail.com",
    "googlemail.com", "msn.com",
}


@dataclass
class ContactData:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None


def extract_contact(from_header: str) -> Optional[ContactData]:
    """Return a ContactData from a raw From header, or None if it should be skipped."""
    display_name, email_addr = parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()

    if any(pattern in email_addr for pattern in _SKIP_PATTERNS):
        return None

    contact = ContactData(email=email_addr)

    # Parse first / last name from display name
    name = display_name.strip().strip('"\'')
    if name:
        parts = name.split()
        if len(parts) >= 2:
            contact.first_name = parts[0]
            contact.last_name = " ".join(parts[1:])
        else:
            contact.first_name = parts[0] if parts else None

    # Derive company from domain when it is not a generic provider
    domain = email_addr.split("@")[1]
    if domain not in _GENERIC_DOMAINS:
        raw = domain.split(".")[0]
        contact.company = re.sub(r"[-_]", " ", raw).title()

    return contact
