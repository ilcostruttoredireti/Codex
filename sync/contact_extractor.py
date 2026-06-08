"""Parse sender name, email address, and company from raw email headers."""
import re
from email.utils import parseaddr
from typing import Optional, Tuple

# Well-known personal/generic domains we don't extract a company from
_GENERIC_DOMAINS = {
    "gmail.com", "yahoo.com", "yahoo.it", "hotmail.com", "hotmail.it",
    "outlook.com", "live.com", "live.it", "icloud.com", "me.com",
    "aol.com", "protonmail.com", "proton.me", "mail.com", "gmx.com",
    "gmx.net", "web.de", "libero.it", "alice.it", "tiscali.it",
    "virgilio.it", "tin.it", "fastwebnet.it", "email.it", "katamail.com",
    "msn.com", "googlemail.com",
}

# Subdomains we strip before extracting the company name
_IGNORED_SUBDOMAINS = {"www", "mail", "smtp", "email", "m"}


class SenderInfo:
    __slots__ = ("email", "firstname", "lastname", "company", "domain")

    def __init__(
        self,
        email: str,
        firstname: Optional[str],
        lastname: Optional[str],
        company: Optional[str],
        domain: str,
    ):
        self.email = email
        self.firstname = firstname
        self.lastname = lastname
        self.company = company
        self.domain = domain

    def __repr__(self) -> str:
        return (
            f"SenderInfo(email={self.email!r}, firstname={self.firstname!r}, "
            f"lastname={self.lastname!r}, company={self.company!r})"
        )


def parse_sender(from_header: str) -> Optional[SenderInfo]:
    """
    Parse a raw 'From:' header value into a SenderInfo.

    Handles formats like:
      - "John Doe <john@example.com>"
      - "John <john@example.com>"
      - "john@example.com"
    Returns None if the header is empty or the address is invalid.
    """
    display_name, addr = parseaddr(from_header)
    if not addr or "@" not in addr:
        return None

    email = addr.strip().lower()
    domain = email.split("@", 1)[1]

    firstname, lastname = _split_name(display_name, email)
    company = _company_from_domain(domain)

    return SenderInfo(
        email=email,
        firstname=firstname or None,
        lastname=lastname or None,
        company=company,
        domain=domain,
    )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _split_name(display_name: str, email: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (firstname, lastname) from the display name."""
    name = display_name.strip()
    # Remove surrounding quotes
    name = re.sub(r'^["\']|["\']$', "", name).strip()

    if not name:
        # Try to infer a first name from the local part before dots/plus
        local = email.split("@")[0]
        parts = re.split(r"[._+\-]", local)
        if parts and len(parts[0]) > 1:
            return parts[0].capitalize(), None
        return None, None

    # Remove anything in angle brackets that might have leaked in
    name = re.sub(r"<[^>]+>", "", name).strip()

    parts = name.split()
    if len(parts) == 1:
        return parts[0].title(), None
    return parts[0].title(), " ".join(parts[1:]).title()


def _company_from_domain(domain: str) -> Optional[str]:
    """Derive a company name from the email domain, or None for personal domains."""
    if domain.lower() in _GENERIC_DOMAINS:
        return None

    parts = domain.lower().split(".")
    # Strip known non-informative subdomains from the left
    while parts and parts[0] in _IGNORED_SUBDOMAINS:
        parts = parts[1:]

    if len(parts) < 2:
        return None

    # For country-code TLDs (co.uk, com.br, org.it …) take the third-to-last part
    _SECONDARY_TLDS = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in _SECONDARY_TLDS:
        name_part = parts[-3]
    else:
        name_part = parts[-2]

    return name_part.replace("-", " ").replace("_", " ").title()
