"""Parse contact information from Gmail message headers."""

import re
from dataclasses import dataclass, field
from email.headerregistry import Address
from typing import Optional


# Domains that belong to free email providers — not suitable as company names
_FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "mac.com", "libero.it", "virgilio.it", "tin.it", "tiscali.it",
    "fastwebnet.it", "alice.it", "protonmail.com", "protonmail.ch",
    "pm.me", "tutanota.com", "zoho.com", "aol.com", "msn.com",
}


@dataclass
class ContactInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""


def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles single-word names."""
    parts = display_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from a domain (e.g. acme.com → Acme)."""
    if not domain or domain in _FREE_DOMAINS:
        return ""
    # Strip common TLDs and capitalise
    name = re.sub(r"\.(com|net|org|io|co|it|eu|biz|info|de|fr|es|uk)$", "", domain, flags=re.I)
    # Handle sub-domains: keep last meaningful part
    parts = name.split(".")
    name = parts[-1] if parts else name
    return name.capitalize()


def parse_from_header(from_header: str) -> Optional[ContactInfo]:
    """
    Parse a raw From header like:
        "Mario Rossi <mario@acme.com>"
        "mario@acme.com"
    Returns None if the header is empty or unparseable.
    """
    if not from_header:
        return None

    from_header = from_header.strip()

    # Try RFC-2822 "Display Name <addr>" format first
    match = re.match(r'^"?([^"<>]+?)"?\s*<([^>]+)>$', from_header)
    if match:
        display_name = match.group(1).strip()
        email_addr = match.group(2).strip().lower()
    elif "@" in from_header and "<" not in from_header:
        display_name = ""
        email_addr = from_header.lower()
    else:
        return None

    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@", 1)[1]
    first_name, last_name = _split_name(display_name) if display_name else ("", "")
    company = _company_from_domain(domain)

    return ContactInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )
