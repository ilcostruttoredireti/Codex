import re
from email.utils import parseaddr
from typing import Optional, Tuple

PERSONAL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "live.com", "icloud.com", "me.com", "aol.com", "msn.com",
    "protonmail.com", "proton.me", "tutanota.com", "mail.com",
    "yahoo.it", "libero.it", "virgilio.it", "tiscali.it",
}


def _split_name(display_name: str) -> Tuple[str, str]:
    parts = display_name.strip().split(None, 1)
    first = parts[0] if parts else ""
    last = parts[1] if len(parts) > 1 else ""
    return first, last


def _company_from_domain(domain: str) -> Optional[str]:
    if not domain or domain.lower() in PERSONAL_DOMAINS:
        return None
    # "mail.acme.co.uk" → take second-to-last meaningful segment
    parts = domain.lower().split(".")
    # Skip common subdomains like "mail", "smtp", "mx"
    stem = parts[-2] if len(parts) >= 2 else parts[0]
    return stem.capitalize() if stem else None


def extract_contact(from_header: str) -> dict:
    """
    Parse a raw From header and return a dict with:
      email, first_name, last_name, company, domain
    Returns empty dict if no valid email is found.
    """
    display_name, email_addr = parseaddr(from_header)
    email_addr = (email_addr or "").lower().strip()
    if not email_addr or "@" not in email_addr:
        return {}

    domain = email_addr.split("@")[1]

    if display_name:
        first_name, last_name = _split_name(display_name)
    else:
        local = email_addr.split("@")[0]
        # john.doe+tag → "john doe"
        local = re.sub(r"\+.*$", "", local)
        local = re.sub(r"[._\-]", " ", local)
        parts = [p.capitalize() for p in local.split() if p]
        first_name = parts[0] if parts else ""
        last_name = " ".join(parts[1:]) if len(parts) > 1 else ""

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "company": _company_from_domain(domain),
        "domain": domain,
    }
