"""Parse Gmail From: headers into structured SenderContact objects."""
from __future__ import annotations
import re
from email.headerregistry import Address
from typing import Optional
from models import SenderContact

# Domains that are generic webmail providers — not company domains
_GENERIC_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it", "live.com",
    "live.it", "icloud.com", "me.com", "mac.com", "libero.it", "virgilio.it",
    "tiscali.it", "tin.it", "alice.it", "email.it", "msn.com",
})

_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*<([^>]+)>$')


def _split_name(display_name: str) -> tuple[Optional[str], Optional[str]]:
    """Split 'Mario Rossi' → ('Mario', 'Rossi')."""
    name = display_name.strip().strip('"')
    parts = name.split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], None
    return None, None


def _company_from_domain(domain: str) -> Optional[str]:
    if not domain or domain in _GENERIC_DOMAINS:
        return None
    # Strip common TLDs and format nicely: "acme.com" → "Acme"
    base = domain.split(".")[0]
    return base.capitalize() if base else None


def parse_from_header(raw_from: str) -> SenderContact:
    """
    Parse a raw From header into a SenderContact.

    Handles formats:
      - "Mario Rossi <mario@acme.com>"
      - mario@acme.com
      - "mario@acme.com" <mario@acme.com>
    """
    raw_from = raw_from.strip()
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email_addr: str = raw_from

    m = _NAME_RE.match(raw_from)
    if m:
        display_name = m.group(1).strip()
        email_addr = m.group(2).strip()
        # Ignore display names that are just the email address repeated
        if "@" not in display_name:
            first_name, last_name = _split_name(display_name)
    elif "<" in raw_from:
        # Fallback: extract angle-bracket address
        start = raw_from.index("<") + 1
        end = raw_from.index(">")
        email_addr = raw_from[start:end].strip()

    email_addr = email_addr.lower()
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""
    company = _company_from_domain(domain)

    return SenderContact(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        raw_from=raw_from,
    )
