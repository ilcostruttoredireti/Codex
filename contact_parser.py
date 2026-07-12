"""Parse sender contact info from raw email From headers."""

import re
from dataclasses import dataclass
from email.headerregistry import Address
from email.utils import parseaddr


# Prefixes that indicate automated / no-reply senders — skip these
_SKIP_PREFIXES = (
    "noreply", "no-reply", "no_reply", "nobody", "donotreply",
    "do-not-reply", "mailer-daemon", "postmaster", "bounce",
    "notifications", "alerts", "notification", "unsubscribe",
)

# Domains that are bulk-mailing platforms with no real human sender
_SKIP_DOMAINS = {
    "e.feedspot.com", "academia-mail.com", "discord.com",
    "skool.com", "serpapi.com", "thomsonreuters.com",
}


@dataclass
class ContactInfo:
    email: str
    first_name: str
    last_name: str
    company: str  # inferred from domain when no display name


def _infer_name(local: str, display: str) -> tuple[str, str]:
    """Return (first_name, last_name) from display name or local part."""
    if display and display.lower() != local:
        parts = display.strip().split()
        first = parts[0].capitalize() if parts else ""
        last = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""
        return first, last

    # Fall back to parsing the local part (e.g. "andreaconta1968" → "Andrea Conta")
    cleaned = re.sub(r"\d+", "", local).replace(".", " ").replace("_", " ").replace("-", " ")
    # Split camelCase
    cleaned = re.sub(r"([a-z])([A-Z])", r"\1 \2", cleaned)
    parts = [p.capitalize() for p in cleaned.split() if p]
    first = parts[0] if parts else local.capitalize()
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first, last


def _infer_company(domain: str, display: str) -> str:
    """Return a human-readable company name from domain or display name."""
    if display and "@" not in display:
        return display

    # Strip TLD and common subdomains, title-case the rest
    parts = domain.split(".")
    skip = {"com", "net", "org", "io", "co", "it", "es", "de", "fr", "ai"}
    name_parts = [p for p in parts if p not in skip and len(p) > 2]
    if not name_parts:
        return domain

    raw = name_parts[-1]
    # Split camelCase / words glued together
    raw = re.sub(r"([a-z])([A-Z])", r"\1 \2", raw)
    raw = raw.replace("-", " ").replace("_", " ")
    return " ".join(w.capitalize() for w in raw.split())


def parse_sender(from_header: str) -> ContactInfo | None:
    """
    Parse a raw From header string into ContactInfo.
    Returns None for automated / no-reply senders.
    """
    display, raw_email = parseaddr(from_header)
    raw_email = raw_email.strip().lower()

    if not raw_email or "@" not in raw_email:
        return None

    local, domain = raw_email.split("@", 1)

    # Filter automated senders
    if any(local.startswith(p) for p in _SKIP_PREFIXES):
        return None
    if any(local == p for p in _SKIP_PREFIXES):
        return None
    if domain in _SKIP_DOMAINS:
        return None

    first, last = _infer_name(local, display)
    company = _infer_company(domain, display if display and display != first else "")

    # gmail.com / personal domains → no meaningful company
    if domain in {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com"}:
        company = ""

    return ContactInfo(
        email=raw_email,
        first_name=first,
        last_name=last,
        company=company,
    )


def deduplicate(contacts: list[ContactInfo]) -> list[ContactInfo]:
    """Return contacts with unique emails (first occurrence wins)."""
    seen: set[str] = set()
    out = []
    for c in contacts:
        if c.email not in seen:
            seen.add(c.email)
            out.append(c)
    return out
