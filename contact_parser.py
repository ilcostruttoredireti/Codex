from email.utils import parseaddr
from typing import Optional

from config import PERSONAL_EMAIL_DOMAINS


def parse_sender(from_header: str) -> dict:
    """
    Parse a raw From: header and return a dict with all contact fields.

    Example input:  "Mario Rossi <mario.rossi@acme.com>"
    Example output: {
        "email":     "mario.rossi@acme.com",
        "name":      "Mario Rossi",
        "firstname": "Mario",
        "lastname":  "Rossi",
        "company":   "Acme",
        "domain":    "acme.com",
    }
    """
    display_name, email = parseaddr(from_header)
    email = email.lower().strip()

    if not email or "@" not in email:
        return {}

    firstname, lastname = _split_name(display_name.strip())
    domain = email.split("@", 1)[1]
    company = _company_from_domain(domain)

    return {
        "email": email,
        "name": display_name.strip(),
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def _split_name(name: str) -> tuple:
    """Split 'First Last' into (first, last). Handles single-word names."""
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    """
    Derive a company name from an email domain.

    Returns empty string for personal/free email providers so we don't
    pollute HubSpot with "Gmail" or "Yahoo" as the company name.
    """
    domain = domain.lower()
    if domain in PERSONAL_EMAIL_DOMAINS:
        return ""

    # Strip subdomain if present (e.g. "mail.acme.com" → "acme.com")
    parts = domain.split(".")
    if len(parts) > 2:
        # Keep only the last two segments (SLD + TLD) for lookup, but use SLD for label
        company_slug = parts[-2]
    else:
        company_slug = parts[0]

    return company_slug.capitalize()
