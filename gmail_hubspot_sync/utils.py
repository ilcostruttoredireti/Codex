"""
Pure helper functions with no external dependencies.
Extracted here so they can be unit-tested without Google/HubSpot SDKs.
"""
from __future__ import annotations

import re

import config


def parse_name(display_name: str) -> tuple[str, str]:
    """Split 'First Last' → ('First', 'Last'). Handles edge cases."""
    parts = display_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def company_from_domain(domain: str) -> str:
    """'acme.com' → 'Acme', 'mail.bigcorp.io' → 'Bigcorp'."""
    parts = domain.split(".")
    name = parts[-2] if len(parts) >= 2 else parts[0]
    return name.capitalize()


def extract_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a From: header value."""
    match = re.match(r'^"?([^"<]*?)"?\s*<([^>]+)>', from_header)
    if match:
        return match.group(1).strip(), match.group(2).strip().lower()
    bare = re.search(r"[\w.+-]+@[\w.-]+\.\w+", from_header)
    if bare:
        return "", bare.group(0).lower()
    return "", from_header.strip().lower()


def should_ignore(email: str) -> bool:
    local, _, domain = email.partition("@")
    if domain in config.IGNORED_DOMAINS:
        return True
    for prefix in config.IGNORED_PREFIXES:
        if local.startswith(prefix):
            return True
    return False
