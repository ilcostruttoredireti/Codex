"""Helpers for parsing sender names/emails and extracting company domains."""

import re
from typing import Tuple, Optional

# Domains that belong to free email providers — not useful as company names
FREE_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "tutanota.com", "fastmail.com", "zoho.com", "yandex.com",
    "libero.it", "alice.it", "tin.it", "virgilio.it", "tiscali.it",
    "email.it", "katamail.com",
}

_RFC_PATTERN = re.compile(r'"?([^"<]*?)"?\s*<([^>]+)>')


def parse_sender(raw: str) -> Tuple[Optional[str], str]:
    """Return (display_name_or_None, email_address) from a raw From header."""
    raw = (raw or "").strip()
    m = _RFC_PATTERN.search(raw)
    if m:
        name = m.group(1).strip() or None
        email = m.group(2).strip().lower()
    else:
        name = None
        email = raw.lower()
    return name, email


def split_name(display_name: Optional[str]) -> Tuple[str, str]:
    """Split a display name into (firstname, lastname).

    Handles:
    - "John Doe"       → ("John", "Doe")
    - "Doe, John"      → ("John", "Doe")
    - "john.doe"       → ("John", "Doe")
    - "John"           → ("John", "")
    """
    if not display_name:
        return "", ""

    name = display_name.strip()

    # "Last, First" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1], parts[0]

    # "first.last" or "first_last" format
    if re.match(r"^[a-z]+[._][a-z]+$", name, re.I):
        sep = "." if "." in name else "_"
        parts = name.split(sep, 1)
        return parts[0].capitalize(), parts[1].capitalize()

    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def extract_domain(email: str) -> str:
    """Return the domain part of an email address."""
    if "@" in email:
        return email.split("@", 1)[1].lower()
    return ""


def domain_to_company(domain: str) -> Optional[str]:
    """Convert a domain to a plausible company name, or None for free providers."""
    if not domain or domain in FREE_EMAIL_PROVIDERS:
        return None
    # Strip common TLDs and subdomains to get the brand name
    parts = domain.split(".")
    if parts[0] in ("mail", "email", "smtp", "info", "support", "hello", "team"):
        parts = parts[1:]
    brand = parts[0] if parts else domain
    return brand.replace("-", " ").title()


def is_ignorable(email: str, ignored_domains: set) -> bool:
    """Return True when an email should be skipped entirely."""
    if not email or "@" not in email:
        return True
    local, domain = email.split("@", 1)
    local_lower = local.lower()
    domain_lower = domain.lower()

    if domain_lower in ignored_domains:
        return True
    for keyword in ("noreply", "no-reply", "donotreply", "mailer-daemon",
                    "postmaster", "bounce", "notification", "daemon",
                    "automated", "auto-confirm", "unsubscribe"):
        if keyword in local_lower or keyword in domain_lower:
            return True
    return False
