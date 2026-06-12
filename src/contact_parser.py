"""
Parses sender information from Gmail messages, including forwarded emails.
"""

import re
from dataclasses import dataclass, field
from typing import Optional


# Common free email domains that shouldn't be used as company names
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "live.com", "msn.com", "aol.com", "protonmail.com", "libero.it",
    "tiscali.it", "alice.it", "virgilio.it", "tin.it", "fastwebnet.it",
}


@dataclass
class ContactInfo:
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    company: Optional[str] = None
    source_message_id: Optional[str] = None
    source_subject: Optional[str] = None
    raw_sender: Optional[str] = None
    tags: list = field(default_factory=list)


def parse_name(display_name: str) -> tuple[Optional[str], Optional[str]]:
    """Split a display name into first and last name."""
    name = display_name.strip().strip('"\'')
    if not name:
        return None, None
    parts = name.split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> Optional[str]:
    """Derive a company name from an email domain."""
    if domain.lower() in FREE_EMAIL_DOMAINS:
        return None
    # Remove common TLDs and subdomains, capitalize each word
    base = domain.split(".")[0]
    return base.replace("-", " ").replace("_", " ").title()


def parse_sender(sender_str: str) -> Optional[ContactInfo]:
    """
    Parse a 'From:' header or forwarded-sender line into ContactInfo.

    Handles formats:
      - "Name Surname <email@domain.com>"
      - "<email@domain.com>"
      - "email@domain.com"
      - "Da \"Name\" email@domain.com" (Italian forward format)
    """
    if not sender_str:
        return None

    sender_str = sender_str.strip()

    # Try "Name <email>" format
    match = re.match(r'^"?([^"<>]+?)"?\s*<([^>]+)>$', sender_str)
    if match:
        display_name = match.group(1).strip()
        email = match.group(2).strip().lower()
        first, last = parse_name(display_name)
        domain = email.split("@")[-1] if "@" in email else ""
        return ContactInfo(
            email=email,
            first_name=first,
            last_name=last,
            company=company_from_domain(domain),
            raw_sender=sender_str,
            tags=["Inbound Gmail"],
        )

    # Try bare email
    match = re.match(r'^([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})$', sender_str)
    if match:
        email = match.group(1).lower()
        domain = email.split("@")[-1]
        return ContactInfo(
            email=email,
            company=company_from_domain(domain),
            raw_sender=sender_str,
            tags=["Inbound Gmail"],
        )

    return None


# Patterns for extracting original sender from Italian-style forwards
_FW_PATTERNS = [
    # Da "Name" email@domain.com
    re.compile(r'Da\s+"([^"]+)"\s+([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.IGNORECASE),
    # Da: Name <email>  or  From: Name <email>
    re.compile(r'(?:Da|From):\s*"?([^"<\n]+?)"?\s*<([^>]+)>', re.IGNORECASE),
    # Da: email  or  From: email
    re.compile(r'(?:Da|From):\s*([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})', re.IGNORECASE),
]


def extract_original_sender_from_forward(snippet: str) -> Optional[ContactInfo]:
    """
    When the direct sender is a forwarder (e.g. redazione@...), attempt to
    extract the original sender from the email snippet/body.
    """
    for pattern in _FW_PATTERNS:
        match = pattern.search(snippet)
        if match:
            groups = match.groups()
            if len(groups) == 2:
                name_or_email, email_or_none = groups
                # Determine which group is the email
                if "@" in email_or_none:
                    email = email_or_none.strip().lower()
                    first, last = parse_name(name_or_email)
                else:
                    email = name_or_email.strip().lower()
                    first, last = None, None
            else:
                email = groups[0].strip().lower()
                first, last = None, None

            domain = email.split("@")[-1] if "@" in email else ""
            return ContactInfo(
                email=email,
                first_name=first,
                last_name=last,
                company=company_from_domain(domain),
                raw_sender=match.group(0),
                tags=["Inbound Gmail"],
            )
    return None
