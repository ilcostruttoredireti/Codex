from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

# Free/personal email providers — domain is not used as company name
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "mail.com", "gmx.com", "gmx.net", "yandex.com", "yandex.ru",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "tin.it",
}

# Local-parts that indicate automated / no-reply senders
_SKIP_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounce+", "auto-reply",
    "notifications", "notification", "newsletter", "unsubscribe",
)


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str


def extract_contact_from_sender(from_header: str) -> Optional[ContactInfo]:
    """
    Parse a From header value and return ContactInfo, or None if the sender
    should be ignored (automated / noreply / unparseable).
    """
    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    local, domain = email_addr.split("@", 1)

    if any(local.startswith(prefix) for prefix in _SKIP_PREFIXES):
        return None

    first_name: Optional[str] = None
    last_name: Optional[str] = None

    if display_name:
        parts = display_name.strip().split()
        if len(parts) >= 2:
            first_name = parts[0]
            last_name = " ".join(parts[1:])
        elif len(parts) == 1:
            first_name = parts[0]

    company: Optional[str] = None
    if domain not in _PERSONAL_DOMAINS:
        root = domain.split(".")[0]
        company = root.capitalize()

    return ContactInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )
