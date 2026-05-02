from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

# Free/personal email domains — company name is not derived from these
_FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "live.com", "icloud.com", "me.com",
    "protonmail.com", "proton.me", "libero.it", "tiscali.it", "virgilio.it",
    "fastwebnet.it", "tin.it", "alice.it",
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str


def extract_contact(from_header: str) -> Optional[ContactInfo]:
    """Parse a RFC 2822 From header and return structured contact info."""
    display_name, email = parseaddr(from_header)
    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    domain = email.split("@", 1)[1]

    first_name: Optional[str] = None
    last_name: Optional[str] = None
    if display_name:
        name = display_name.strip().strip('"')
        parts = name.split(maxsplit=1)
        first_name = parts[0] if parts else None
        last_name = parts[1] if len(parts) > 1 else None

    # Derive a company hint from the domain for non-personal providers
    company: Optional[str] = None
    if domain not in _FREE_DOMAINS:
        # "acme.co.uk" → "Acme"
        company = domain.split(".")[0].capitalize()

    return ContactInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )
