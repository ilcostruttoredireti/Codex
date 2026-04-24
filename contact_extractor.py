"""
Extracts structured contact information from a raw Gmail message dict.
"""

import re
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.utils import parseaddr


# Domains that belong to free/generic providers and should not be used as
# company names.
_GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.de",
    "hotmail.com", "hotmail.it", "hotmail.co.uk",
    "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me",
    "libero.it", "virgilio.it", "alice.it", "tin.it",
    "tiscali.it", "fastwebnet.it",
    "aol.com", "msn.com",
    "yandex.com", "yandex.ru",
}


@dataclass
class ContactInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    raw_sender: str = ""
    subject: str = ""
    message_id: str = ""


def _header_value(headers: list[dict], name: str) -> str:
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _split_name(display_name: str) -> tuple[str, str]:
    """Best-effort split of a display name into (first, last)."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from a domain when it's not generic."""
    if not domain or domain.lower() in _GENERIC_DOMAINS:
        return ""
    # Strip common TLD and capitalise: "acme.com" → "Acme"
    base = domain.split(".")[0]
    return base.capitalize()


def extract_contact(message: dict) -> ContactInfo | None:
    """
    Parse a Gmail message dict (format='full') and return a ContactInfo.
    Returns None when the From header is missing or the address is invalid.
    """
    payload = message.get("payload", {})
    headers = payload.get("headers", [])

    from_header = _header_value(headers, "From")
    if not from_header:
        return None

    display_name, email_address = parseaddr(from_header)
    email_address = email_address.strip().lower()

    # Basic sanity check
    if not re.match(r"[^@]+@[^@]+\.[^@]+", email_address):
        return None

    domain = email_address.split("@")[-1]
    first_name, last_name = _split_name(display_name)
    company = _company_from_domain(domain)
    subject = _header_value(headers, "Subject")

    return ContactInfo(
        email=email_address,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        raw_sender=from_header,
        subject=subject,
        message_id=message.get("id", ""),
    )
