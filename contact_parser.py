"""Parse email From: headers into structured contact data."""

import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional, Tuple

# Free/personal email domains — we won't derive a company name from these
_PERSONAL_DOMAINS: frozenset = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it", "live.com",
    "live.it", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com",
    "mail.com", "protonmail.com", "proton.me", "tutanota.com", "fastmail.com",
    "libero.it", "tiscali.it", "virgilio.it", "alice.it", "tin.it",
    "email.it", "katamail.com", "interfree.it", "ymail.com",
})

# Local-part prefixes that indicate automated/system senders
_SKIP_LOCALS: tuple = (
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "newsletter", "newsletters", "news",
    "automated", "auto", "robot", "system",
)


@dataclass
class ContactData:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]


def parse_sender(from_header: str) -> Optional[ContactData]:
    """Parse a From: header into a ContactData, or None if the sender should be skipped."""
    display_name, email_addr = parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    if "." not in email_addr.split("@", 1)[1]:
        return None  # malformed domain

    local, domain = email_addr.split("@", 1)

    # Skip automated/system senders
    if any(local.startswith(prefix) for prefix in _SKIP_LOCALS):
        return None

    first_name, last_name = _split_display_name(display_name)
    company = _domain_to_company(domain)

    return ContactData(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
    )


def _split_display_name(name: str) -> Tuple[Optional[str], Optional[str]]:
    name = name.strip().strip("\"'")
    if not name:
        return None, None
    parts = name.split(maxsplit=1)
    first = parts[0] if parts else None
    last = parts[1] if len(parts) > 1 else None
    return first, last


def _domain_to_company(domain: str) -> Optional[str]:
    if domain in _PERSONAL_DOMAINS:
        return None
    # Strip TLD(s) and humanise: "acme-corp.com" → "Acme Corp"
    name = domain.split(".")[0]
    name = re.sub(r"[-_]", " ", name)
    return name.title() or None
