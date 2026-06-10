import re
from dataclasses import dataclass
from typing import Optional

# Consumer email providers — company is not inferred from domain
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "live.it",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "proton.me", "mail.com", "gmx.com", "gmx.net", "msn.com", "libero.it",
    "tiscali.it", "virgilio.it", "alice.it",
}

# Local-part patterns that indicate automated/no-reply senders
_SKIP_PATTERNS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces",
    "notifications", "notification", "automated", "auto-reply",
    "newsletter", "unsubscribe",
]


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    raw_from: str


def parse_from_header(from_header: str) -> Optional[ContactInfo]:
    """
    Parse a raw From: header into structured contact info.
    Returns None for no-reply addresses and unparseable headers.
    """
    from_header = from_header.strip()

    # Match "Display Name <email@domain>" or bare "email@domain"
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>\s*$', from_header)
    if match:
        display_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        email = from_header.lower().strip()
        display_name = ""

    # Basic email sanity check
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return None

    local, domain = email.rsplit("@", 1)

    # Skip automated senders
    if any(pattern in local for pattern in _SKIP_PATTERNS):
        return None

    # Company from domain (skip personal providers)
    company: Optional[str] = None
    if domain not in _PERSONAL_DOMAINS:
        root = domain.split(".")[0]
        company = root.capitalize() if root else None

    # Split display name into first / last
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    if display_name:
        parts = display_name.split()
        if parts:
            first_name = parts[0]
            if len(parts) > 1:
                last_name = " ".join(parts[1:])

    return ContactInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        raw_from=from_header,
    )
