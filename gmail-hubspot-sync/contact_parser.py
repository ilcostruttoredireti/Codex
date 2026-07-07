"""
Parse Gmail sender strings into structured contact data.
"""

import re
from dataclasses import dataclass


# Patterns that indicate automated/noreply senders — skip these
_SKIP_PATTERNS = re.compile(
    r"^(no.?reply|noreply|notifications?.noreply|do.?not.?reply|"
    r"mailer.daemon|postmaster|bounce|auto.?reply|daemon|"
    r"ads.noreply|invitations)@",
    re.IGNORECASE,
)

# Domains that are pure notification services — contacts not useful
_SKIP_DOMAINS = frozenset(
    [
        "linkedin.com",
        "revolut.com",
        "fastweb.it",
        "discord.com",
        "hotel-bb.com",
        "moneya.es",
        "google.com",
        "facebook.com",
        "twitter.com",
        "instagram.com",
    ]
)


@dataclass
class Contact:
    email: str
    firstname: str
    lastname: str
    company: str
    domain: str


def parse_sender(raw_sender: str) -> Contact | None:
    """
    Parse a Gmail sender string into a Contact.

    Accepts:
      - "Name <email@domain.com>"
      - "email@domain.com"

    Returns None for automated/noreply senders.
    """
    # Extract display name and email
    match = re.match(r"^(.*?)\s*<([^>]+)>$", raw_sender.strip())
    if match:
        display_name = match.group(1).strip().strip('"')
        email = match.group(2).strip().lower()
    else:
        display_name = ""
        email = raw_sender.strip().lower()

    if not _is_valid_email(email):
        return None
    if _SKIP_PATTERNS.match(email):
        return None

    domain = email.split("@", 1)[1]
    if domain in _SKIP_DOMAINS:
        return None

    firstname, lastname = _split_name(display_name, email)
    company = _infer_company(domain)

    return Contact(
        email=email,
        firstname=firstname,
        lastname=lastname,
        company=company,
        domain=domain,
    )


def _is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^@]+@[^@]+\.[^@]+$", email))


def _split_name(display_name: str, email: str) -> tuple[str, str]:
    if display_name:
        parts = display_name.split(None, 1)
        return parts[0], parts[1] if len(parts) > 1 else ""

    # Fall back to email local part: "first.last@domain" → ("First", "Last")
    local = email.split("@")[0]
    parts = re.split(r"[._\-]+", local)
    parts = [p.capitalize() for p in parts if p]
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _infer_company(domain: str) -> str:
    """Best-effort company name from domain (strip TLD and common prefixes)."""
    # Remove known subdomains
    parts = domain.split(".")
    # Drop "www", "mail", "info", "email", etc.
    drop = {"www", "mail", "info", "email", "app", "go", "get"}
    parts = [p for p in parts[:-1] if p not in drop]  # exclude TLD
    if not parts:
        return domain
    name = parts[-1]
    return name.replace("-", " ").title()
