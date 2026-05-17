"""Email parsing utilities for extracting sender contact data."""

import re
from dataclasses import dataclass, field
from typing import Optional


GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "libero.it",
    "tiscali.it", "alice.it", "virgilio.it", "tin.it",
}

AUTOMATED_SENDER_PATTERNS = [
    r"@.*facebookmail\.com$",
    r"@.*mailchimp\.com$",
    r"notification@",
    r"noreply@",
    r"no-reply@",
    r"mailer-daemon@",
    r"postmaster@",
    r"bounce.*@",
    r"donotreply@",
]

# Italian forwarded email pattern: Da "Name" email@domain or Da: Name <email>
_FWD_PATTERNS = [
    re.compile(
        r'Da\s+"([^"]+)"\s+([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})',
        re.IGNORECASE,
    ),
    re.compile(
        r"Da:\s+([^\n<]+?)\s*<([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>",
        re.IGNORECASE,
    ),
    re.compile(
        r"From:\s+([^\n<]+?)\s*<([\w.+\-]+@[\w.\-]+\.[a-zA-Z]{2,})>",
        re.IGNORECASE,
    ),
]


@dataclass
class ContactInfo:
    email: str
    firstname: str = ""
    lastname: str = ""
    full_name: str = ""
    company: str = ""
    domain: str = ""
    thread_id: str = ""
    subject: str = ""

    @property
    def is_generic_domain(self) -> bool:
        return self.domain.lower() in GENERIC_EMAIL_DOMAINS


def is_automated(email: str) -> bool:
    email_lower = email.lower()
    return any(re.search(p, email_lower) for p in AUTOMATED_SENDER_PATTERNS)


def domain_from_email(email: str) -> str:
    parts = email.split("@")
    return parts[1].lower() if len(parts) == 2 else ""


def _company_from_domain(domain: str) -> str:
    """Best-effort company name from a domain."""
    if domain in GENERIC_EMAIL_DOMAINS:
        return ""
    parts = domain.split(".")
    # drop common TLDs and subdomains like www, mail, press, ufficio
    skip = {"www", "mail", "press", "ufficio", "info", "it", "com", "org",
            "net", "eu", "ch", "gov", "edu", "co"}
    parts = [p for p in parts if p.lower() not in skip]
    if not parts:
        return ""
    name = parts[0].replace("-", " ").replace("_", " ")
    return name.title()


def _split_name(full_name: str) -> tuple[str, str]:
    """Split a full name into (firstname, lastname)."""
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].title(), ""
    return parts[0].title(), " ".join(parts[1:]).title()


def parse_forwarded_sender(body: str) -> Optional[tuple[str, str]]:
    """Return (name, email) extracted from a forwarded message body, or None."""
    for pattern in _FWD_PATTERNS:
        m = pattern.search(body or "")
        if m:
            name = m.group(1).strip().strip('"')
            email = m.group(2).strip()
            if not is_automated(email):
                return name, email
    return None


def extract_contact(
    sender: str,
    subject: str,
    thread_id: str,
    body: str = "",
) -> Optional[ContactInfo]:
    """
    Extract a ContactInfo from an email.

    For forwarded emails (body contains Da/From header), parse the original
    sender from the body. Otherwise use the direct sender.
    """
    email = sender.strip().lower()

    # Try to extract from forwarded body first
    parsed = parse_forwarded_sender(body)
    if parsed:
        fwd_name, fwd_email = parsed
        if not is_automated(fwd_email):
            email = fwd_email.lower()
            full_name = fwd_name
        else:
            return None
    else:
        if is_automated(email):
            return None
        full_name = ""

    domain = domain_from_email(email)
    firstname, lastname = _split_name(full_name)
    company = "" if domain in GENERIC_EMAIL_DOMAINS else _company_from_domain(domain)

    return ContactInfo(
        email=email,
        firstname=firstname,
        lastname=lastname,
        full_name=full_name,
        company=company,
        domain=domain,
        thread_id=thread_id,
        subject=subject,
    )
