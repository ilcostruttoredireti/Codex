"""Parse Gmail From: headers into structured contact data."""

import re
from dataclasses import dataclass
from typing import Optional, Tuple

PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "hotmail.com",
    "hotmail.it", "outlook.com", "outlook.it", "icloud.com", "aol.com",
    "live.com", "live.it", "msn.com", "protonmail.com", "protonmail.ch",
    "mail.com", "yandex.com", "yandex.ru", "gmx.com", "zoho.com",
    "fastmail.com", "me.com", "mac.com", "libero.it", "virgilio.it",
    "alice.it", "tin.it", "tiscali.it", "email.it", "inwind.it", "iol.it",
}

# Local-part prefixes that indicate automated/system senders
_AUTO_PREFIXES = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "bounce", "bounces", "notification",
    "notifications", "newsletter", "automated", "automailer", "robot",
    "daemon", "system", "alert", "alerts",
)

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None


def parse_from_header(from_header: str) -> Optional[ContactInfo]:
    """Return a ContactInfo for a From: header, or None if the sender should be skipped."""
    if not from_header:
        return None

    from_header = from_header.strip()

    # "Display Name <email@host.tld>" format
    bracket_match = re.search(r"<([^>]+@[^>]+)>", from_header)
    if bracket_match:
        email = bracket_match.group(1).strip().lower()
        raw_name = from_header[: from_header.rfind("<")].strip().strip("\"'")
    else:
        email = re.sub(r"\s+", "", from_header).lower()
        raw_name = ""

    if not email or "@" not in email:
        return None

    if not _EMAIL_RE.match(email):
        return None

    local, domain = email.split("@", 1)

    # Drop automated senders
    if any(local.startswith(p) or local == p for p in _AUTO_PREFIXES):
        return None

    first_name, last_name = _split_name(raw_name, local)

    # Derive company from domain; ignore personal/free-mail domains
    company: Optional[str] = None
    if domain not in PERSONAL_DOMAINS:
        root = domain.split(".")[0]
        company = root.replace("-", " ").replace("_", " ").title()

    return ContactInfo(
        email=email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def _split_name(display_name: str, local_part: str) -> Tuple[Optional[str], Optional[str]]:
    if display_name:
        display_name = re.sub(r"\s+", " ", display_name).strip()
        parts = display_name.split(None, 1)
        first = parts[0] if parts else None
        last = parts[1] if len(parts) > 1 else None
        return first, last

    # Fallback: infer from local part (e.g. "john.doe" → "John", "Doe")
    segments = re.split(r"[._\-]", local_part)
    if len(segments) >= 2 and all(s.isalpha() for s in segments[:2]):
        return segments[0].capitalize(), segments[1].capitalize()

    return None, None
