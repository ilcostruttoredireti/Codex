from email.utils import parseaddr
from dataclasses import dataclass
from typing import Optional

GENERIC_DOMAINS = {
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
    'icloud.com', 'me.com', 'mac.com', 'live.com', 'msn.com',
    'protonmail.com', 'tutanota.com', 'fastmail.com', 'aol.com',
    'mail.com', 'zoho.com', 'yandex.com', 'gmx.com', 'libero.it',
    'virgilio.it', 'tiscali.it', 'alice.it', 'tim.it', 'wind.it',
    'yahoo.it', 'googlemail.com',
}

AUTOMATED_PREFIXES = {
    'noreply', 'no-reply', 'donotreply', 'do-not-reply',
    'notifications', 'notification', 'mailer-daemon', 'postmaster',
    'bounce', 'bounces', 'support', 'info', 'automated', 'auto',
    'newsletter', 'updates', 'alert', 'alerts', 'system', 'daemon',
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None


def extract_contact(from_header: str) -> Optional[ContactInfo]:
    """Parse a From header and return a ContactInfo, or None if invalid/automated."""
    if not from_header:
        return None

    display_name, email_address = parseaddr(from_header)

    if not email_address or '@' not in email_address:
        return None

    email_address = email_address.lower().strip()
    local, domain = email_address.split('@', 1)

    if any(local == prefix or local.startswith(prefix + '+') for prefix in AUTOMATED_PREFIXES):
        return None

    first_name, last_name = _split_name(display_name.strip())
    company = _domain_to_company(domain)

    return ContactInfo(
        email=email_address,
        first_name=first_name,
        last_name=last_name,
        company=company,
    )


def _split_name(display_name: str) -> tuple:
    name = display_name.strip().strip('"\'')
    if not name:
        return None, None
    parts = name.split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], ' '.join(parts[1:])


def _domain_to_company(domain: str) -> Optional[str]:
    if domain.lower() in GENERIC_DOMAINS:
        return None
    parts = domain.split('.')
    if len(parts) >= 2:
        return parts[-2].capitalize()
    return None
