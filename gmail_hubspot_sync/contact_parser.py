"""Parse email From: headers into structured contact fields."""
from __future__ import annotations

import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional


SKIP_LOCAL_PREFIXES = (
    "noreply",
    "no-reply",
    "donotreply",
    "do-not-reply",
    "notification",
    "notifications",
    "mailer-daemon",
    "mailer",
    "bounce",
    "bounces",
    "postmaster",
    "newsletter",
    "news",
    "autoresponder",
    "auto-reply",
)

SKIP_DOMAIN_EXACT = frozenset(
    [
        "facebookmail.com",
        "facebookmail.net",
        "bounce.mail.facebook.com",
    ]
)

SKIP_DOMAIN_PREFIXES = ("bounce.", "bounces.", "mailer.", "notifications.", "notify.")

FREE_EMAIL_DOMAINS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.it",
        "yahoo.fr",
        "yahoo.es",
        "hotmail.com",
        "hotmail.it",
        "outlook.com",
        "outlook.it",
        "live.com",
        "live.it",
        "icloud.com",
        "me.com",
        "mac.com",
        "protonmail.com",
        "proton.me",
        "libero.it",
        "virgilio.it",
        "tiscali.it",
        "alice.it",
        "tin.it",
        "email.it",
    ]
)


@dataclass
class ContactInfo:
    email: str
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    should_skip: bool = False
    skip_reason: str = ""


def parse_sender(from_header: str) -> ContactInfo:
    """Parse a From: header string into a ContactInfo.

    Handles formats:
    - "John Doe <john@example.com>"
    - "John <john@example.com>"
    - "john@example.com"
    """
    display_name, email_addr = parseaddr(from_header.strip())

    if not email_addr:
        # Try treating the whole thing as a bare email
        if "@" in from_header:
            email_addr = from_header.strip()
            display_name = ""
        else:
            return ContactInfo(email="", should_skip=True, skip_reason="no_email")

    email_addr = email_addr.lower().strip()

    if not _is_valid_email(email_addr):
        return ContactInfo(email=email_addr, should_skip=True, skip_reason="invalid_email")

    local, _, domain = email_addr.partition("@")
    domain = domain.lower()

    if _should_skip(local, domain):
        return ContactInfo(
            email=email_addr,
            domain=domain,
            should_skip=True,
            skip_reason="automated_sender",
        )

    firstname, lastname = _parse_name(display_name, local)
    company = _derive_company(domain)

    return ContactInfo(
        email=email_addr,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
    )


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email))


def _should_skip(local: str, domain: str) -> bool:
    local_lower = local.lower()
    # Strip subaddress (plus addressing) before checking
    base_local = local_lower.split("+")[0]

    for prefix in SKIP_LOCAL_PREFIXES:
        if base_local == prefix:
            return True

    if domain in SKIP_DOMAIN_EXACT:
        return True

    for pat in SKIP_DOMAIN_PREFIXES:
        if domain.startswith(pat):
            return True

    return False


def _parse_name(display_name: str, local: str) -> tuple[Optional[str], Optional[str]]:
    """Split display name into (firstname, lastname)."""
    name = display_name.strip()
    if not name:
        return None, None

    # Remove surrounding quotes
    name = name.strip('"').strip("'").strip()
    if not name:
        return None, None

    parts = name.split(None, 1)
    if len(parts) == 2:
        return _clean_name_part(parts[0]), _clean_name_part(parts[1])
    return _clean_name_part(parts[0]), None


def _clean_name_part(s: str) -> Optional[str]:
    cleaned = s.strip().strip('"').strip("'")
    return cleaned if cleaned else None


def _derive_company(domain: str) -> Optional[str]:
    """Derive company name from email domain.

    Returns None for free/personal email providers.
    For business domains, returns a human-readable company name.
    """
    if domain in FREE_EMAIL_DOMAINS:
        return None

    # Split into parts and remove common TLDs
    parts = domain.split(".")
    if len(parts) < 2:
        return domain.title()

    # Walk from the right, skipping short TLD-like segments (≤3 chars) to find
    # the first meaningful name segment.  This handles:
    #   "co.uk", "com.br", Italian provinces like "tn.it", "mc.it", etc.
    tld_like_threshold = 3
    idx = len(parts) - 1
    while idx > 0 and len(parts[idx]) <= tld_like_threshold:
        idx -= 1

    company_part = parts[idx]
    return company_part.replace("-", " ").title()
