"""Pure helper functions with no external dependencies."""

import re

_FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "live.com", "me.com",
    "aol.com", "mail.com", "gmx.com", "ymail.com",
}


def parse_name(display_name: str) -> tuple[str, str]:
    """Split a display name into (first, last)."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def domain_to_company(domain: str) -> str:
    """Convert a domain to a best-effort company name. Returns '' for free email domains."""
    if domain.lower() in _FREE_DOMAINS:
        return ""
    name = re.sub(r"\.(com|org|net|io|co|it|eu|de|fr|es|uk|biz|info)$", "", domain.lower())
    name = name.split(".")[-1]
    return name.capitalize()


def extract_email_parts(email: str) -> tuple[str, str]:
    """Return (local_part, domain) from an email address."""
    parts = email.lower().split("@", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")
