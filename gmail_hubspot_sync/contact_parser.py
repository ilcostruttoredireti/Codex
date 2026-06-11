"""
Parse contact information from email headers and body.
Handles both direct emails and forwarded messages (e.g. via redazione@latestata.it).
"""
import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Optional

# Pattern: Da "First Last" email@domain.com (Italian forward header)
_FWD_IT = re.compile(
    r'Da\s+"?([^"<\n]+?)"?\s+<?([\w.+\-]+@[\w.\-]+)>?',
    re.IGNORECASE,
)
# Pattern: From: "First Last" <email@domain.com> (English forward header)
_FWD_EN = re.compile(
    r'From:\s+"?([^"<\n]+?)"?\s+<([\w.+\-]+@[\w.\-]+)>',
    re.IGNORECASE,
)


@dataclass
class Contact:
    email: str
    firstname: str = ""
    lastname: str = ""
    company: str = ""
    source: str = "Gmail"

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1].lower() if "@" in self.email else ""

    @property
    def full_name(self) -> str:
        return f"{self.firstname} {self.lastname}".strip()

    def to_hubspot_props(self) -> dict:
        props: dict = {"email": self.email}
        if self.firstname:
            props["firstname"] = self.firstname
        if self.lastname:
            props["lastname"] = self.lastname
        if self.company:
            props["company"] = self.company
        props["hs_lead_source"] = "OTHER"  # closest standard value
        props["source_from_gmail"] = "true"  # custom property if configured
        return props


def _split_name(full: str) -> tuple[str, str]:
    """Split a full name into (firstname, lastname). Handles 'Di Mitrio' composites."""
    parts = full.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> str:
    """Derive a rough company name from an email domain."""
    # Remove TLD(s) — take first label
    label = domain.split(".")[0]
    # Convert hyphens/underscores to spaces, title-case
    return label.replace("-", " ").replace("_", " ").title()


def parse_sender(sender_header: str, snippet: str = "") -> Optional[Contact]:
    """
    Build a Contact from a raw From/sender header and optional body snippet.
    Returns None if the sender should be skipped.
    """
    name_raw, email_raw = parseaddr(sender_header)
    email_raw = email_raw.strip().lower()
    if not email_raw or "@" not in email_raw:
        return None

    firstname, lastname = _split_name(name_raw) if name_raw else ("", "")
    domain = email_raw.split("@")[-1]
    company = _domain_to_company(domain) if domain else ""

    contact = Contact(
        email=email_raw,
        firstname=firstname,
        lastname=lastname,
        company=company,
    )

    # If this is a forwarding address, try to extract the real sender from snippet
    if snippet:
        original = _extract_forwarded_sender(snippet)
        if original:
            return original

    return contact


def _extract_forwarded_sender(text: str) -> Optional[Contact]:
    """Look for forwarded-message sender patterns inside body text."""
    for pattern in (_FWD_IT, _FWD_EN):
        m = pattern.search(text)
        if m:
            name_raw = m.group(1).strip().strip('"')
            email_raw = m.group(2).strip().lower()
            if "@" not in email_raw:
                continue
            firstname, lastname = _split_name(name_raw)
            domain = email_raw.split("@")[-1]
            company = _domain_to_company(domain)
            return Contact(
                email=email_raw,
                firstname=firstname,
                lastname=lastname,
                company=company,
            )
    return None


def should_skip(email: str, skip_domains: set, own_emails: set) -> bool:
    """Return True if this sender should not be synced."""
    email = email.lower()
    if email in own_emails:
        return True
    domain = email.split("@")[-1]
    if domain in skip_domains:
        return True
    # mailer-daemon and delivery service addresses
    if "mailer-daemon" in email or "postmaster" in email:
        return True
    return False
