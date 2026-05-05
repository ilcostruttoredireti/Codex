"""
Pure functions for parsing Gmail sender headers into structured contacts.
No external dependencies — importable without Google/HubSpot SDKs.
"""

from dataclasses import dataclass
from email.utils import parseaddr
from typing import Optional

IGNORED_DOMAINS = {
    "noreply.github.com",
    "mailer.notion.so",
    "e.atlassian.com",
    "bounce.linkedin.com",
}


@dataclass
class SenderContact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    domain: str = ""
    message_id: str = ""
    subject: str = ""
    received_at: str = ""


def extract_company_from_domain(domain: str) -> str:
    """Best-effort company name from domain: 'acme.co.uk' → 'Acme'."""
    suffixes = {"com", "net", "org", "io", "co", "uk", "it", "de", "fr",
                "eu", "ai", "app", "dev"}
    parts = [p for p in domain.split(".") if p.lower() not in suffixes]
    return parts[0].capitalize() if parts else domain


def parse_sender(
    from_header: str,
    msg_id: str = "",
    subject: str = "",
    received_at: str = "",
) -> Optional[SenderContact]:
    """Parse a 'From:' header into a SenderContact, or None if invalid/ignored."""
    display_name, email = parseaddr(from_header)
    email = email.strip().lower()

    if not email or "@" not in email:
        return None

    domain = email.split("@")[1]
    if domain in IGNORED_DOMAINS or email.startswith("noreply"):
        return None

    parts = display_name.strip().split(" ", 1)
    first = parts[0].capitalize() if parts[0] else ""
    last = parts[1].strip().capitalize() if len(parts) > 1 else ""

    return SenderContact(
        email=email,
        first_name=first,
        last_name=last,
        company=extract_company_from_domain(domain),
        domain=domain,
        message_id=msg_id,
        subject=subject,
        received_at=received_at,
    )
