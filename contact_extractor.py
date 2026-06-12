"""Parse sender info from a raw email From: header."""
import re
from email.utils import parseaddr
from config import PERSONAL_DOMAINS, AUTOMATED_PREFIXES


def extract_contact(from_header: str) -> dict | None:
    """
    Returns a dict with email/firstname/lastname/domain/company,
    or None if the sender should be skipped (automated, invalid).
    """
    name, email = parseaddr(from_header)

    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    local, domain = email.rsplit("@", 1)

    if any(local == prefix or local.startswith(prefix + "+") for prefix in AUTOMATED_PREFIXES):
        return None

    firstname, lastname = _split_name(name.strip())
    company = _domain_to_company(domain)

    return {
        "email": email,
        "firstname": firstname,
        "lastname": lastname,
        "domain": domain,
        "company": company,
    }


def _split_name(name: str) -> tuple[str, str]:
    name = re.sub(r'["\']', "", name).strip()
    parts = name.split() if name else []
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> str:
    if domain in PERSONAL_DOMAINS:
        return ""
    parts = domain.split(".")
    # For mail.acme.com → acme; for acme.com → acme
    raw = parts[-2] if len(parts) >= 2 else parts[0]
    return re.sub(r"[-_]", " ", raw).title()
