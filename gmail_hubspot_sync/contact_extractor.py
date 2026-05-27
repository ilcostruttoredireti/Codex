"""Extract structured contact info from a raw Gmail message dict."""

import re
from dataclasses import dataclass, field
from email.headerregistry import Address
from email.utils import parseaddr
from typing import Optional

from config import IGNORED_DOMAINS, IGNORED_LOCAL_PARTS


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    domain: Optional[str] = None
    subject: Optional[str] = None
    message_id: Optional[str] = None


def _header_value(message: dict, name: str) -> Optional[str]:
    for h in message.get("payload", {}).get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return None


def _split_name(display_name: str) -> tuple[Optional[str], Optional[str]]:
    parts = display_name.strip().split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> Optional[str]:
    """Best-effort: turn 'acme.com' into 'Acme'."""
    bare = domain.split(".")[0]
    if not bare or len(bare) < 2:
        return None
    return bare.capitalize()


def _should_ignore(local: str, domain: str) -> bool:
    if domain in IGNORED_DOMAINS:
        return True
    if local.lower() in IGNORED_LOCAL_PARTS:
        return True
    if any(kw in local.lower() for kw in ("noreply", "no-reply", "donotreply", "bounce")):
        return True
    return False


def extract_sender(message: dict) -> Optional[SenderContact]:
    from_header = _header_value(message, "From")
    if not from_header:
        return None

    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    local, domain = email_addr.split("@", 1)

    if _should_ignore(local, domain):
        return None

    first, last = _split_name(display_name) if display_name else (None, None)
    company = _domain_to_company(domain)

    return SenderContact(
        email=email_addr,
        first_name=first,
        last_name=last,
        company=company,
        domain=domain,
        subject=_header_value(message, "Subject"),
        message_id=message.get("id"),
    )
