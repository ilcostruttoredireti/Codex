import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import List, Optional

from .config import FREE_EMAIL_DOMAINS

# Matches forwarded-email "From" lines in Italian or English bodies:
#   Da "Name" email@domain.com
#   Da "Name" <email@domain.com>
#   From "Name" <email@domain.com>
#   From: Name <email@domain.com>
_FORWARD_FROM_RE = re.compile(
    r'(?:Da|From)[:\s]+"?([^"<\n]+?)"?\s+<?([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?',
    re.IGNORECASE,
)


@dataclass
class SenderContact:
    email: str
    first_name: Optional[str]
    last_name: Optional[str]
    company: Optional[str]
    domain: str
    message_id: str
    subject: str


def parse_sender(raw_from: str, message_id: str, subject: str) -> Optional[SenderContact]:
    """Extract contact data from a raw 'From' header value."""
    display_name, email_address = parseaddr(raw_from)
    if not email_address or "@" not in email_address:
        return None

    email_address = email_address.strip().lower()
    domain = email_address.split("@")[1]
    first_name, last_name = _split_name(display_name)
    company = _infer_company(domain)

    return SenderContact(
        email=email_address,
        first_name=first_name,
        last_name=last_name,
        company=company,
        domain=domain,
        message_id=message_id,
        subject=subject,
    )


def parse_forwarded_senders(body_text: str, message_id: str, subject: str) -> List[SenderContact]:
    """
    Parse original sender(s) from a forwarded email body.
    Returns a list because a body may contain multiple forwarded messages.
    """
    contacts = []
    seen_emails: set[str] = set()

    for match in _FORWARD_FROM_RE.finditer(body_text):
        display_name = match.group(1).strip()
        email_address = match.group(2).strip().lower()

        if email_address in seen_emails or "@" not in email_address:
            continue
        seen_emails.add(email_address)

        domain = email_address.split("@")[1]
        first_name, last_name = _split_name(display_name)
        company = _infer_company(domain) or _company_from_display_name(display_name)

        contacts.append(SenderContact(
            email=email_address,
            first_name=first_name,
            last_name=last_name,
            company=company,
            domain=domain,
            message_id=message_id,
            subject=subject,
        ))

    return contacts


def _split_name(display_name: str) -> tuple[Optional[str], Optional[str]]:
    name = display_name.strip()
    name = re.sub(r'^["\']|["\']$', "", name)
    if not name:
        return None, None

    parts = name.split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def _infer_company(domain: str) -> Optional[str]:
    if domain in FREE_EMAIL_DOMAINS:
        return None
    root = re.sub(r"^(mail|email|smtp)\.", "", domain)
    company_name = root.split(".")[0].replace("-", " ").title()
    return company_name


def _company_from_display_name(display_name: str) -> Optional[str]:
    """Heuristic: if display name looks like an org (no single word), use it as company."""
    name = display_name.strip().strip('"\'')
    # If it contains digits or multiple words that look institutional, use it
    if len(name.split()) >= 2 and not re.match(r'^[A-Z][a-z]+ [A-Z][a-z]+$', name):
        return name[:100]
    return None
