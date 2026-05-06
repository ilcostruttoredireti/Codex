import re
import logging
from typing import Optional


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return logging.getLogger("gmail_hubspot_sync")


def extract_email_parts(raw_email: str) -> tuple[str, Optional[str]]:
    """
    Parse 'John Doe <john@example.com>' or 'john@example.com'.
    Returns (email_address, display_name_or_None).
    """
    raw_email = raw_email.strip()
    match = re.match(r"^(.+?)\s*<([^>]+)>$", raw_email)
    if match:
        name = match.group(1).strip().strip('"').strip("'")
        email = match.group(2).strip().lower()
        return email, name or None
    email = raw_email.lower()
    return email, None


def parse_name(display_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """Split 'First Last' into (first, last). Returns (None, None) if no name."""
    if not display_name:
        return None, None
    parts = display_name.strip().split(None, 1)
    first = parts[0] if parts else None
    last = parts[1] if len(parts) > 1 else None
    return first, last


def company_from_domain(domain: str) -> str:
    """Convert 'acme.com' → 'Acme'."""
    name = domain.split(".")[0]
    return name.capitalize()


def is_valid_email(email: str) -> bool:
    return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email))
