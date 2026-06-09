import re
import email.header
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    full_name: Optional[str]
    company: Optional[str]
    domain: str


_SKIP_LOCAL_PARTS = frozenset({
    'noreply', 'no-reply', 'donotreply', 'do-not-reply',
    'notifications', 'notification', 'mailer-daemon',
    'postmaster', 'bounce', 'bounces', 'admin', 'abuse',
    'unsubscribe', 'newsletter', 'marketing', 'automated',
})

_GENERIC_DOMAINS = frozenset({
    'gmail.com', 'yahoo.com', 'yahoo.it', 'hotmail.com', 'hotmail.it',
    'outlook.com', 'live.com', 'icloud.com', 'me.com', 'mac.com',
    'aol.com', 'protonmail.com', 'proton.me', 'fastmail.com',
    'zoho.com', 'yandex.com', 'yandex.ru', 'mail.com', 'gmx.com',
    'libero.it', 'virgilio.it', 'alice.it', 'tin.it', 'tiscali.it',
    'email.com', 'inbox.com',
})


def _decode_header(value: str) -> str:
    parts = email.header.decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            result.append(part)
    return ''.join(result)


def parse_sender(from_header: str) -> Optional[ContactInfo]:
    """Parse a Gmail From header into a ContactInfo, or None to skip."""
    if not from_header:
        return None

    from_header = _decode_header(from_header).strip()

    match = re.match(r'^(.+?)\s*<([^>]+)>$', from_header)
    if match:
        display_name = match.group(1).strip().strip('"\'')
        raw_email = match.group(2).strip().lower()
    else:
        display_name = ''
        raw_email = from_header.lower()

    if '@' not in raw_email:
        return None

    local, domain = raw_email.split('@', 1)

    if local in _SKIP_LOCAL_PARTS:
        return None

    first_name, last_name = _split_name(display_name)
    company = _company_from_domain(domain)

    return ContactInfo(
        email=raw_email,
        first_name=first_name,
        last_name=last_name,
        full_name=display_name or None,
        company=company,
        domain=domain,
    )


def _split_name(full_name: str) -> Tuple[Optional[str], Optional[str]]:
    if not full_name:
        return None, None
    parts = full_name.strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], ' '.join(parts[1:])


def _company_from_domain(domain: str) -> Optional[str]:
    if domain in _GENERIC_DOMAINS:
        return None
    # Take the second-to-last label: "acme" from "acme.com" or "mail.acme.com"
    labels = domain.rstrip('.').split('.')
    if len(labels) >= 2:
        return labels[-2].capitalize()
    return domain.capitalize()
