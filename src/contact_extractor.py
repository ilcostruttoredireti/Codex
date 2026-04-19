"""Extract structured contact data from a raw Gmail 'From' header."""

import re
from dataclasses import dataclass, field
from email.utils import parseaddr


# Domains treated as personal/free email providers – not used as company names
_FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "mac.com", "protonmail.com", "proton.me", "tutanota.com",
    "libero.it", "virgilio.it", "tiscali.it", "fastwebnet.it",
    "aol.com", "msn.com", "yandex.com", "yandex.ru",
}

_COMMON_PREFIXES = {"mr", "mrs", "ms", "dr", "prof", "ing", "dott", "dott.ssa"}


@dataclass
class ContactInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    company: str = ""
    domain: str = ""


def extract_contact(from_header: str) -> ContactInfo | None:
    """
    Parse a Gmail 'From' header and return a ContactInfo.
    Returns None if the header has no valid email address.
    """
    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()

    if not email_addr or "@" not in email_addr:
        return None

    domain = email_addr.split("@")[1]
    company = _company_from_domain(domain)
    first_name, last_name = _split_display_name(display_name)

    return ContactInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        full_name=display_name.strip(),
        company=company,
        domain=domain,
    )


def _company_from_domain(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    if domain in _FREE_DOMAINS:
        return ""
    # Strip subdomains (e.g. mail.company.com → company.com)
    parts = domain.split(".")
    if len(parts) > 2:
        parts = parts[-2:]
    root = parts[0]
    return root.replace("-", " ").replace("_", " ").title()


def _split_display_name(display_name: str) -> tuple[str, str]:
    """
    Heuristically split a display name into (first_name, last_name).
    Handles: 'Mario Rossi', 'Rossi, Mario', 'Mario A. Rossi', etc.
    """
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""

    # Remove common courtesy prefixes
    tokens = name.split()
    if tokens and tokens[0].lower().rstrip(".") in _COMMON_PREFIXES:
        tokens = tokens[1:]

    if not tokens:
        return "", ""

    # 'Rossi, Mario' format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]

    if len(tokens) == 1:
        return tokens[0], ""

    # First token → first name, last token → last name
    return tokens[0], tokens[-1]
