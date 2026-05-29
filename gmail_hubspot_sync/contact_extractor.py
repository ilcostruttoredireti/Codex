import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

COMMON_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "live.com", "aol.com", "protonmail.com",
    "me.com", "mac.com",
}


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    raw_from: str = ""


def extract_sender(from_header: str) -> Optional[SenderContact]:
    """Parse a From header into a SenderContact."""
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()

    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]
    contact = SenderContact(email=email_addr, domain=domain, raw_from=from_header)

    if display_name:
        _parse_display_name(display_name.strip(), contact)

    if domain not in COMMON_DOMAINS:
        contact.company = _company_from_domain(domain)

    return contact


def _parse_display_name(name: str, contact: SenderContact) -> None:
    # Strip surrounding quotes
    name = name.strip('"\'')
    if not name:
        return

    parts = name.split()
    if len(parts) == 1:
        contact.first_name = parts[0]
    elif len(parts) >= 2:
        # Handle "Last, First" format
        if parts[0].endswith(","):
            contact.last_name = parts[0].rstrip(",")
            contact.first_name = " ".join(parts[1:])
        else:
            contact.first_name = parts[0]
            contact.last_name = " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Convert a domain into a human-readable company name."""
    # Remove TLD and subdomains
    parts = domain.split(".")
    # Take second-to-last part (e.g. "acme" from "mail.acme.com")
    if len(parts) >= 2:
        name = parts[-2]
    else:
        name = parts[0]
    # Capitalise each word in hyphenated names
    return " ".join(word.capitalize() for word in name.split("-"))
