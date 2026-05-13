"""Parse sender info from Gmail message metadata headers."""

import re
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.utils import parseaddr

# Domains so generic we never use them as company names.
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "live.com", "aol.com", "protonmail.com",
    "proton.me", "mail.com", "msn.com", "ymail.com",
}


@dataclass
class SenderContact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    subject: str = ""
    date: str = ""


def parse_sender(message: dict) -> SenderContact | None:
    """
    Extract SenderContact from a Gmail message metadata dict.
    Returns None if no valid From address is found.
    """
    headers = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    from_header = headers.get("From", "")
    if not from_header:
        return None

    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.strip().lower()

    if not _is_valid_email(email_addr):
        return None

    firstname, lastname = _split_name(display_name)
    company = _company_from_domain(email_addr)

    return SenderContact(
        email=email_addr,
        firstname=firstname,
        lastname=lastname,
        company=company,
        subject=headers.get("Subject", ""),
        date=headers.get("Date", ""),
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _is_valid_email(addr: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr))


def _split_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'); handles edge cases."""
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""

    # Remove parenthetical suffixes like "(via LinkedIn)"
    name = re.sub(r"\s*\(.*?\)", "", name).strip()

    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(email_addr: str) -> str:
    """
    Derive a company name from the email domain.
    Returns '' for common consumer domains.
    """
    domain = email_addr.split("@")[-1].lower()
    if domain in _GENERIC_DOMAINS:
        return ""
    # Strip TLD and capitalise: acme.co → Acme
    company_raw = domain.split(".")[0]
    return company_raw.capitalize()
