"""Parse sender info from email headers and map to HubSpot contact fields."""

import re
from email.utils import parseaddr

# Free/consumer email domains — don't infer company from these
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "icloud.com", "me.com", "aol.com",
    "protonmail.com", "proton.me", "mail.com", "gmx.com", "yandex.com",
    "fastmail.com", "libero.it", "tiscali.it", "virgilio.it",
}


def parse_sender(from_header: str) -> tuple[str, str]:
    """Return (display_name, email_address) from a raw From header."""
    name, addr = parseaddr(from_header)
    return name.strip(), addr.strip().lower()


def split_name(full_name: str) -> tuple[str, str]:
    """Return (firstname, lastname) from a display name."""
    parts = full_name.strip().split(None, 1) if full_name.strip() else []
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def company_from_domain(email_addr: str) -> str | None:
    """
    Infer company name from the email domain.
    Returns None for generic/consumer domains.
    """
    if "@" not in email_addr:
        return None
    domain = email_addr.split("@", 1)[1].lower()
    if domain in _GENERIC_DOMAINS:
        return None
    # Take the SLD: "acme-corp.co.uk" → "acme-corp" → "Acme Corp"
    sld = domain.split(".")[0]
    return re.sub(r"[-_]", " ", sld).title()


def build_contact_properties(name: str, email_addr: str) -> dict:
    """Return a dict of HubSpot contact properties derived from sender data."""
    firstname, lastname = split_name(name)
    company = company_from_domain(email_addr)
    props: dict = {
        "email": email_addr,
        "lead_source": "Gmail",
    }
    if firstname:
        props["firstname"] = firstname
    if lastname:
        props["lastname"] = lastname
    if company:
        props["company"] = company
    return props


def missing_fields(existing_props: dict, candidate_props: dict) -> dict:
    """Return only the fields from candidate that are absent/empty in existing."""
    return {
        k: v
        for k, v in candidate_props.items()
        if k != "email" and not existing_props.get(k)
    }
