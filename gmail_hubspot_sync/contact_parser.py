"""Extract contact information from Gmail message headers and body snippets."""
import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Contact:
    email: str
    first_name: str = ""
    last_name: str = ""
    company: str = ""
    source: str = "Gmail"
    tags: list = field(default_factory=lambda: ["Inbound Gmail"])

    @property
    def domain(self) -> str:
        return self.email.split("@")[1] if "@" in self.email else ""

    @property
    def is_generic_domain(self) -> bool:
        generic = {
            "gmail.com", "yahoo.it", "yahoo.com", "libero.it",
            "alice.it", "hotmail.com", "hotmail.it", "outlook.com",
            "icloud.com", "tiscali.it", "virgilio.it",
        }
        return self.domain.lower() in generic


# Pattern: 'Da "Name Surname" email@domain.tld' used in forwarded emails
_FWD_PATTERN = re.compile(
    r'Da\s+"([^"]+)"\s+<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>?',
    re.IGNORECASE,
)

# Pattern: 'From: Name Surname <email@domain.tld>'
_FROM_PATTERN = re.compile(
    r'(?:From|Da):\s*([^<\n]+?)\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>',
    re.IGNORECASE,
)


def _split_name(full_name: str) -> tuple[str, str]:
    """Split 'First Last' into (first, last). Handles multiple words."""
    parts = full_name.strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return parts[0] if parts else "", ""


def _company_from_domain(domain: str) -> str:
    """Derive a rough company name from the domain."""
    name = domain.split(".")[0]
    return name.replace("-", " ").replace("_", " ").title()


def parse_sender(sender_header: str) -> Optional[Contact]:
    """Parse a Gmail 'From' header value like 'Name <email>' or just 'email'."""
    sender_header = sender_header.strip()
    match = re.match(
        r'^"?([^"<]+?)"?\s*<([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})>$',
        sender_header,
    )
    if match:
        name_raw, email = match.group(1).strip(), match.group(2).strip().lower()
        first, last = _split_name(name_raw)
        c = Contact(email=email, first_name=first, last_name=last)
    elif re.match(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$', sender_header):
        c = Contact(email=sender_header.lower())
    else:
        return None

    if not c.is_generic_domain and not c.company:
        c.company = _company_from_domain(c.domain)
    return c


def extract_forwarded_contacts(snippet: str) -> list[Contact]:
    """Pull original sender contacts out of Italian forwarded-email snippets."""
    contacts = []
    for match in _FWD_PATTERN.finditer(snippet):
        name_raw, email = match.group(1).strip(), match.group(2).strip().lower()
        first, last = _split_name(name_raw)
        c = Contact(email=email, first_name=first, last_name=last)
        if not c.is_generic_domain:
            c.company = _company_from_domain(c.domain)
        contacts.append(c)
    return contacts
