"""Pure utility functions shared by gmail_client and hubspot_client."""

from __future__ import annotations

import re

_NAME_RE = re.compile(r'^"?([^"<]+?)"?\s*<[^>]+>$')
_COMPANY_RE = re.compile(r"^(?:www\.)?([^.]+)\.[a-z]{2,}(?:\.[a-z]{2})?$", re.I)

FREEMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "hotmail.co.uk", "outlook.com", "live.com", "icloud.com", "me.com",
    "aol.com", "protonmail.com", "proton.me", "mail.com", "gmx.com",
    "yandex.com", "yandex.ru", "zoho.com", "tutanota.com",
}


def parse_sender(raw_from: str) -> dict:
    """Parse a raw From header into a structured dict."""
    raw_from = raw_from.strip()
    match = _NAME_RE.match(raw_from)
    if match:
        display_name = match.group(1).strip()
        email_addr = re.search(r"<([^>]+)>", raw_from).group(1).lower()
    else:
        display_name = ""
        email_addr = raw_from.lower()

    parts = display_name.split(None, 1) if display_name else ["", ""]
    first_name = parts[0] if parts[0] else ""
    last_name = parts[1] if len(parts) > 1 else ""

    domain = email_addr.split("@")[1] if "@" in email_addr else ""
    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
    }


def domain_to_company(domain: str) -> str:
    """Best-effort: turn 'acme.com' → 'Acme'. Returns '' for freemail domains."""
    if not domain or domain.lower() in FREEMAIL_DOMAINS:
        return ""
    m = _COMPANY_RE.match(domain)
    if m:
        return m.group(1).capitalize()
    return ""
