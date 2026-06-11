"""Parse Gmail From headers into structured contact data."""

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional


# Public/personal email domains — no useful company info here
_PUBLIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "aol.com", "protonmail.com", "mail.com",
    "gmx.com", "gmx.net", "zoho.com", "yandex.com", "fastmail.com",
    "hey.com", "tutanota.com", "libero.it", "alice.it", "tin.it",
    "virgilio.it", "tiscali.it", "inwind.it", "email.it",
}

# Patterns in the local part that indicate automated/system senders
_NOREPLY_PATTERNS = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "notifications", "notification", "mailer-daemon", "postmaster",
    "bounce", "autoreply", "auto-reply", "support", "newsletter",
    "info", "admin", "contact", "hello", "team",
)


@dataclass
class Contact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None


def parse_sender(from_header: str) -> Optional[Contact]:
    """Parse a Gmail From header into a Contact. Returns None for invalid/automated senders."""
    display_name, email_addr = parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    local_part, domain = email_addr.split("@", 1)

    if any(p in local_part for p in _NOREPLY_PATTERNS):
        return None

    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name and display_name.strip():
        parts = display_name.strip().split()
        if parts:
            first_name = parts[0]
            if len(parts) >= 2:
                last_name = " ".join(parts[1:])

    company = _company_from_domain(domain)

    return Contact(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def _company_from_domain(domain: str) -> Optional[str]:
    """Infer company name from email domain, returns None for public domains."""
    if domain in _PUBLIC_DOMAINS:
        return None

    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]
        return name.replace("-", " ").replace("_", " ").title()

    return None
