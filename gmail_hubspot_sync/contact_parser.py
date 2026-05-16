"""Parses sender info into HubSpot contact property dicts."""

import re
from typing import Optional

from models import SenderInfo

# Domains that belong to free/personal email providers — don't use as company name.
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "mac.com", "aol.com", "protonmail.com", "proton.me",
    "tutanota.com", "libero.it", "tiscali.it", "alice.it", "virgilio.it",
    "fastwebnet.it", "tim.it", "tin.it",
}


def _split_name(full_name: str) -> tuple[str, str]:
    """Best-effort split of 'First Last' into (first, last)."""
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> Optional[str]:
    """Convert 'acme.com' → 'Acme', return None for personal email domains."""
    if domain in _PERSONAL_DOMAINS:
        return None
    # Strip common TLD suffixes and capitalise
    name = re.sub(r"\.(com|org|net|io|co|it|de|fr|es|uk|eu|biz|info)(\.[a-z]{2})?$", "", domain)
    return name.capitalize() if name else None


def build_hubspot_props(sender: SenderInfo, source: str, tag: str) -> dict:
    """Return a dict of HubSpot contact properties derived from the email sender."""
    first, last = _split_name(sender.name)
    company = _domain_to_company(sender.domain)

    props: dict = {
        "email": sender.email,
        "leadsource": source,
    }
    if first:
        props["firstname"] = first
    if last:
        props["lastname"] = last
    if company:
        props["company"] = company

    return props


def merge_props(existing: dict, new_props: dict) -> dict:
    """Return only the fields in new_props that are missing/empty in existing."""
    update = {}
    for key, value in new_props.items():
        if key == "email":
            continue  # never change the primary email key via patch
        if not existing.get(key):
            update[key] = value
    return update
