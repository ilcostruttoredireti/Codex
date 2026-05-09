from email.utils import parseaddr
from config import PERSONAL_EMAIL_DOMAINS


def extract_sender_info(from_header: str) -> dict | None:
    """
    Parse a raw 'From' email header into structured contact fields.
    Returns None if no valid email address can be found.
    """
    display_name, email = parseaddr(from_header)
    if not email:
        return None

    email = email.lower().strip()
    if "@" not in email:
        return None

    domain = email.split("@", 1)[1]
    first_name, last_name = _split_name(display_name)
    company = _company_from_domain(domain)

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "display_name": display_name.strip(),
        "domain": domain,
        "company": company,
    }


def _split_name(display_name: str) -> tuple[str, str]:
    name = display_name.strip().strip('"').strip("'")
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str | None:
    """
    Infer a company name from an email domain.
    Returns None for personal/free-email domains.
    """
    if domain in PERSONAL_EMAIL_DOMAINS:
        return None
    # e.g. "mail.acme.co.uk" → "acme"
    parts = domain.split(".")
    # Strip leading 'mail', 'smtp', 'mx' subdomains
    while len(parts) > 2 and parts[0] in ("mail", "smtp", "mx", "email"):
        parts = parts[1:]
    company_token = parts[0] if parts else domain
    return company_token.capitalize()
