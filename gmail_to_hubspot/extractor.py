"""Extract structured contact data from an email From header."""

import re
from email.utils import parseaddr
from dataclasses import dataclass
from typing import Optional

FREE_EMAIL_DOMAINS: frozenset = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "ymail.com",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "proton.me", "tutanota.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "fastwebnet.it",
    "tin.it", "email.it",
})

# Local-parts that indicate automated / system senders
_IGNORED_LOCALS: frozenset = frozenset({
    "no-reply", "noreply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "bounce", "bounces", "postmaster", "daemon",
    "autoresponder", "autoreply", "mailerdaemon",
    "notifications", "notification",
    "newsletter",
})

# Domain stems (before first ".") that also indicate automated senders
_IGNORED_DOMAIN_STEMS: frozenset = frozenset({
    "no-reply", "noreply", "newsletter", "bounce", "bounces",
    "mailer", "mailerdaemon", "notifications",
})


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str


def _domain_to_company(domain: str) -> str:
    """'acme-corp.com' → 'Acme Corp'."""
    name = domain.split(".")[0]
    return re.sub(r"[-_]", " ", name).title()


def extract_sender(from_header: str) -> Optional[SenderContact]:
    """
    Parse a From header and return a SenderContact.
    Returns None if the address is malformed or should be ignored.
    """
    display_name, email_address = parseaddr(from_header)
    email_address = email_address.lower().strip()

    if not email_address or "@" not in email_address:
        return None

    local, domain = email_address.split("@", 1)

    if local in _IGNORED_LOCALS:
        return None

    if domain.split(".")[0] in _IGNORED_DOMAIN_STEMS:
        return None

    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name:
        parts = display_name.strip().split()
        if len(parts) == 1:
            first_name = parts[0].capitalize()
        elif len(parts) >= 2:
            first_name = parts[0].capitalize()
            last_name = " ".join(p.capitalize() for p in parts[1:])

    company: Optional[str] = None
    if domain not in FREE_EMAIL_DOMAINS:
        company = _domain_to_company(domain)

    return SenderContact(
        email=email_address,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )
