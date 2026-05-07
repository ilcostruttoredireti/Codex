"""Extract structured contact data from a Gmail message."""

import re
import logging
from email.headerregistry import Address
from email.utils import parseaddr

logger = logging.getLogger(__name__)

# Domains that belong to free email providers – not usable as company names
_FREE_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "mac.com", "protonmail.com", "proton.me", "libero.it",
    "virgilio.it", "tiscali.it", "alice.it", "tin.it", "fastwebnet.it",
    "aol.com", "mail.com", "gmx.com", "gmx.net", "web.de",
}


def _header_value(message: dict, name: str) -> str:
    """Pull a header value from the Gmail message metadata."""
    for header in message.get("payload", {}).get("headers", []):
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def extract_sender_info(message: dict) -> dict | None:
    """
    Return a dict with keys:
      email, first_name, last_name, full_name, domain, company
    or None if no valid sender email is found.
    """
    raw_from = _header_value(message, "From")
    if not raw_from:
        return None

    display_name, email_addr = parseaddr(raw_from)
    email_addr = email_addr.strip().lower()

    if not email_addr or "@" not in email_addr:
        return None

    # Skip noreply / automated senders
    local_part = email_addr.split("@")[0]
    if re.match(r"(no.?reply|noreply|do.not.reply|mailer.daemon|postmaster|bounce)", local_part, re.I):
        logger.debug("Skipping automated sender: %s", email_addr)
        return None

    domain = email_addr.split("@")[1]
    company = _domain_to_company(domain)

    first_name, last_name = _split_name(display_name)

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": display_name.strip() or None,
        "domain": domain,
        "company": company,
    }


def _domain_to_company(domain: str) -> str | None:
    """Convert an email domain to a guessed company name, or None for free providers."""
    if domain in _FREE_DOMAINS:
        return None
    # Strip common subdomains and TLD
    parts = domain.split(".")
    if len(parts) >= 2:
        name = parts[-2]  # e.g. "acmecorp" from "mail.acmecorp.com"
        return name.capitalize()
    return domain


def _split_name(display_name: str) -> tuple[str | None, str | None]:
    """Best-effort split of a display name into first / last."""
    name = display_name.strip()
    if not name:
        return None, None

    # Remove surrounding quotes
    name = name.strip('"').strip("'").strip()

    # "Last, First" format
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        return parts[1] or None, parts[0] or None

    parts = name.split()
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])
