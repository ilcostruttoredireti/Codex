from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

# Free/personal email domains — company is not derivable from these
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "protonmail.com",
    "mail.com", "inbox.com", "gmx.com", "yandex.com", "proton.me",
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str
    message_id: str
    subject: str
    received_at: str


def extract_contact(message: dict) -> Optional[ContactInfo]:
    headers = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    from_header = headers.get("From") or headers.get("Reply-To", "")
    if not from_header:
        return None

    display_name, email_address = parseaddr(from_header)
    email_address = email_address.lower().strip()
    if not email_address or "@" not in email_address:
        return None

    domain = email_address.split("@")[1]
    first_name, last_name = _split_name(display_name.strip().strip('"').strip("'"))

    return ContactInfo(
        email=email_address,
        first_name=first_name,
        last_name=last_name,
        company=_company_from_domain(domain),
        domain=domain,
        message_id=message.get("id", ""),
        subject=headers.get("Subject", ""),
        received_at=headers.get("Date", ""),
    )


def _split_name(display_name: str):
    if not display_name:
        return None, None
    parts = display_name.split(maxsplit=1)
    first = parts[0] if parts else None
    last = parts[1] if len(parts) > 1 else None
    return first, last


def _company_from_domain(domain: str) -> Optional[str]:
    if domain in _GENERIC_DOMAINS:
        return None
    # Take the second-to-last label: "acme" from "mail.acme.com" or "acme.com"
    labels = domain.split(".")
    company_label = labels[-2] if len(labels) >= 2 else labels[0]
    return company_label.capitalize()
