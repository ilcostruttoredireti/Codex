"""
Extracts structured contact info from a Gmail message sender field.
"""
import re
from dataclasses import dataclass, field
from email.headerregistry import Address


@dataclass
class ContactInfo:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


# Domains that don't represent real company names
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "mac.com",
    "libero.it", "tiscali.it", "virgilio.it", "alice.it", "tin.it",
    "fastwebnet.it", "windtre.it", "inwind.it",
}


def extract_contact(sender: str) -> ContactInfo | None:
    """
    Parse a Gmail 'From' header like:
      'Mario Rossi <mario.rossi@example.com>'  or  'mario.rossi@example.com'

    Returns None for addresses that should be skipped (no-reply, etc.).
    """
    if not sender:
        return None

    email_addr, display_name = _parse_sender(sender)
    if not email_addr:
        return None

    email_addr = email_addr.lower().strip()
    domain = email_addr.split("@")[-1] if "@" in email_addr else ""

    first_name, last_name = _split_name(display_name, email_addr)
    company = _company_from_domain(domain)

    return ContactInfo(
        email=email_addr,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
    )


def _parse_sender(sender: str) -> tuple[str, str]:
    """Return (email, display_name) from a raw From header value."""
    # Try 'Name <email>' pattern first
    match = re.match(r'^"?([^"<]*)"?\s*<([^>]+)>$', sender.strip())
    if match:
        name = match.group(1).strip().strip('"')
        email = match.group(2).strip()
        return email, name

    # Plain email address
    if "@" in sender:
        return sender.strip(), ""

    return "", ""


def _split_name(display_name: str, email_addr: str) -> tuple[str, str]:
    """
    Derive first / last name from the display name.
    Falls back to parsing the local part of the email address.
    """
    if display_name:
        parts = display_name.split(None, 1)
        first = parts[0].capitalize() if parts else ""
        last = parts[1].title() if len(parts) > 1 else ""
        return first, last

    local = email_addr.split("@")[0]
    # 'mario.rossi' or 'mario_rossi' or 'mariorossi'
    for sep in (".", "_", "-"):
        if sep in local:
            parts = local.split(sep, 1)
            return parts[0].capitalize(), parts[1].capitalize()

    return local.capitalize(), ""


def _company_from_domain(domain: str) -> str:
    """
    Convert 'example.com' → 'Example', 'mail.example.co.uk' → 'Example'
    Returns empty string for generic consumer email providers.
    """
    if not domain or domain in _GENERIC_DOMAINS:
        return ""

    parts = domain.split(".")
    # Remove up to two trailing TLD segments (e.g. '.co.uk' or '.com')
    # and return the last remaining segment as the company name.
    if len(parts) >= 3:
        name = parts[-3]  # e.g. 'example' from 'mail.example.co.uk'
    elif len(parts) == 2:
        name = parts[0]   # e.g. 'example' from 'example.com'
    else:
        name = parts[0]

    return name.capitalize()
