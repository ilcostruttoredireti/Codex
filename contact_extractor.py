import re
from typing import Optional

PERSONAL_DOMAINS = {
    'gmail.com', 'yahoo.com', 'hotmail.com', 'outlook.com', 'live.com',
    'icloud.com', 'protonmail.com', 'me.com', 'mac.com', 'msn.com',
    'aol.com', 'mail.com', 'ymail.com', 'googlemail.com',
}

NOREPLY_PATTERN = re.compile(
    r'^(no.?reply|noreply|donotreply|do.not.reply|mailer|bounce|'
    r'notification|notifications|alert|alerts|newsletter|unsubscribe|'
    r'auto|automated|system|daemon|postmaster|webmaster|admin)@',
    re.IGNORECASE,
)

EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')


def is_valid_email(email: str) -> bool:
    return bool(EMAIL_PATTERN.match(email))


def is_system_email(email: str) -> bool:
    return bool(NOREPLY_PATTERN.match(email))


def extract_company_from_domain(domain: str) -> Optional[str]:
    """Derive a human-readable company name from an email domain."""
    if not domain or domain.lower() in PERSONAL_DOMAINS:
        return None
    parts = domain.split('.')
    # Use the second-level domain (e.g. "acmecorp" from "mail.acmecorp.com")
    candidate = parts[-2] if len(parts) >= 2 else parts[0]
    return candidate.replace('-', ' ').replace('_', ' ').title()


def split_name(full_name: str) -> tuple:
    """Return (first_name, last_name) from a display name string."""
    name = full_name.strip().strip('"\'')
    if not name:
        return '', ''
    parts = name.split(None, 1)
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], parts[1]


def extract_contact(sender_info: dict) -> Optional[dict]:
    """
    Build a structured contact dict from parsed sender info.
    Returns None if the address should be skipped.
    """
    email = sender_info.get('email', '').lower().strip()

    if not is_valid_email(email):
        return None
    if is_system_email(email):
        return None

    domain = email.split('@')[1]
    first_name, last_name = split_name(sender_info.get('name', ''))
    company = extract_company_from_domain(domain)

    return {
        'email': email,
        'first_name': first_name,
        'last_name': last_name,
        'company': company,
        'domain': domain,
    }
