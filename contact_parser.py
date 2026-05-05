from email.utils import parseaddr
from typing import Optional

# Common personal/free email domains that don't represent a company
_PERSONAL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com',
    'icloud.com', 'me.com', 'live.com', 'aol.com', 'msn.com',
    'protonmail.com', 'proton.me', 'tutanota.com', 'tutanota.de',
    'mail.com', 'gmx.com', 'gmx.net', 'ymail.com', 'rocketmail.com',
    'fastmail.com', 'fastmail.fm', 'zoho.com', 'hushmail.com',
    'libero.it', 'virgilio.it', 'tin.it', 'tiscali.it', 'alice.it',
}

# Automated sender prefixes to filter out
_AUTOMATED_PREFIXES = (
    'noreply@', 'no-reply@', 'donotreply@', 'do-not-reply@',
    'mailer-daemon@', 'postmaster@', 'bounce@', 'bounces@',
    'notifications@', 'alerts@', 'automated@',
)


def parse_sender(from_header: str) -> dict:
    """Parse a Gmail From header into structured contact fields."""
    name, email_addr = parseaddr(from_header)
    if not email_addr or '@' not in email_addr:
        return {}

    email_addr = email_addr.lower().strip()
    domain = email_addr.split('@')[1]
    first_name, last_name = _split_name(name.strip())
    company = _domain_to_company(domain)

    return {
        'email': email_addr,
        'full_name': name.strip(),
        'first_name': first_name,
        'last_name': last_name,
        'domain': domain,
        'company': company,
    }


def is_automated(email: str) -> bool:
    """Return True if the address looks like an automated sender."""
    return email.startswith(_AUTOMATED_PREFIXES)


def _split_name(full_name: str) -> tuple[str, str]:
    if not full_name:
        return '', ''
    parts = full_name.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0], ''


def _domain_to_company(domain: str) -> str:
    """Derive a human-readable company name from a business email domain."""
    if not domain or domain in _PERSONAL_DOMAINS:
        return ''
    # Take the first label, replace separators, title-case it
    # e.g. 'acme-corp.co.uk' -> 'Acme Corp'
    name_part = domain.split('.')[0]
    return name_part.replace('-', ' ').replace('_', ' ').title()
