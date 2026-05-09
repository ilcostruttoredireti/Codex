import email.utils
from dataclasses import dataclass
from typing import Optional

FREE_EMAIL_DOMAINS = {
    'gmail.com', 'googlemail.com', 'yahoo.com', 'yahoo.it', 'yahoo.co.uk',
    'hotmail.com', 'hotmail.it', 'outlook.com', 'live.com', 'live.it',
    'icloud.com', 'me.com', 'mac.com', 'aol.com', 'protonmail.com',
    'proton.me', 'mail.com', 'yandex.com', 'yandex.ru', 'zoho.com',
    'libero.it', 'virgilio.it', 'tiscali.it', 'alice.it', 'tin.it',
    'fastwebnet.it', 'email.it',
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str


def extract_contact(from_header: str) -> Optional[ContactInfo]:
    """Parse a From: header and return structured contact information."""
    if not from_header:
        return None

    display_name, addr = email.utils.parseaddr(from_header)

    if not addr or '@' not in addr:
        return None

    addr = addr.lower().strip()
    domain = addr.split('@')[1]

    first_name: Optional[str] = None
    last_name: Optional[str] = None

    name = display_name.strip().strip('"')
    if name and name != addr:
        parts = name.split(maxsplit=1)
        first_name = parts[0] if parts else None
        last_name = parts[1] if len(parts) > 1 else None

    company: Optional[str] = None
    if domain not in FREE_EMAIL_DOMAINS:
        base = domain.split('.')[0]
        company = base.replace('-', ' ').replace('_', ' ').title()

    return ContactInfo(
        email=addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )
