"""Parse sender info from raw email From: header."""

import email.utils
import re

# Free/personal email providers — skip as company source
_PERSONAL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.co.uk",
    "hotmail.com", "hotmail.it", "outlook.com", "live.com", "icloud.com",
    "me.com", "aol.com", "protonmail.com", "proton.me", "mail.com",
    "gmx.com", "gmx.net", "zoho.com", "libero.it", "virgilio.it",
    "alice.it", "tin.it", "tiscali.it", "fastwebnet.it",
}

_CLEAN_RE = re.compile(r"[^a-zA-Z0-9\s]")


def parse_sender(from_header: str) -> dict:
    """
    Parse a raw From: header into structured contact data.

    Returns:
        {
          "email": str,
          "first_name": str,
          "last_name": str,
          "full_name": str,
          "domain": str,
          "company": str,   # empty for personal domains
        }
    """
    display_name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()

    if not addr or "@" not in addr:
        return {}

    domain = addr.split("@", 1)[1]
    first_name, last_name = _split_name(display_name.strip())
    company = _domain_to_company(domain)

    return {
        "email": addr,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": display_name.strip(),
        "domain": domain,
        "company": company,
    }


def _split_name(name: str) -> tuple[str, str]:
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> str:
    if domain in _PERSONAL_DOMAINS:
        return ""
    # Strip TLD(s) and capitalise: "acme.co.uk" → "Acme"
    root = domain.split(".")[0]
    # Remove common prefixes
    root = re.sub(r"^(mail|email|info|noreply|no-reply)$", "", root, flags=re.I)
    return root.capitalize() if root else ""
