from email.utils import parseaddr
from typing import Optional

FREE_EMAIL_PROVIDERS = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "hotmail.com",
        "outlook.com",
        "icloud.com",
        "me.com",
        "live.com",
        "aol.com",
        "protonmail.com",
        "fastmail.com",
        "zoho.com",
        "ymail.com",
        "msn.com",
        "libero.it",
        "alice.it",
        "tiscali.it",
        "virgilio.it",
        "tim.it",
    }
)


def parse_sender(from_header: str) -> Optional[dict]:
    """Parse a From: header into structured contact data."""
    display_name, email_addr = parseaddr(from_header)
    if not email_addr or "@" not in email_addr:
        return None

    email_addr = email_addr.lower().strip()
    display_name = display_name.strip()
    first_name, last_name = _split_name(display_name)
    domain = email_addr.split("@")[1]
    company = _domain_to_company(domain)

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name,
        "domain": domain,
        "company": company,
    }


def _split_name(display_name: str) -> tuple[str, str]:
    if not display_name:
        return "", ""
    parts = display_name.split(" ", 1)
    return parts[0], parts[1] if len(parts) > 1 else ""


def _domain_to_company(domain: str) -> str:
    """Derive a human-readable company name from an email domain."""
    if domain.lower() in FREE_EMAIL_PROVIDERS:
        return ""
    parts = domain.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").replace("_", " ").title()
    return domain
