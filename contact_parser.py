"""Parse sender name and company from an email From header."""

from email.utils import parseaddr
from typing import Optional

# Domains belonging to free/consumer providers — not used as company names
_GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it", "yahoo.co.uk", "yahoo.fr", "yahoo.es",
    "outlook.com", "outlook.it", "hotmail.com", "hotmail.it", "hotmail.co.uk",
    "live.com", "live.it", "msn.com",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "pm.me", "proton.me",
    "tutanota.com", "tutanota.de", "tutamail.com",
    "zoho.com", "zohomail.com",
    "mail.com", "email.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it",
    "fastweb.it", "tin.it", "supereva.it", "inwind.it",
    "gmx.com", "gmx.net", "gmx.de", "gmx.at",
    "web.de", "t-online.de",
    "qq.com", "163.com", "126.com",
}

# Local-part prefixes that indicate automated/system senders to skip
_SKIP_PREFIXES = (
    "noreply", "no-reply", "no_reply",
    "donotreply", "do-not-reply", "do_not_reply",
    "mailer-daemon", "mailer_daemon",
    "postmaster", "bounce", "bounces",
    "notifications", "notification",
    "unsubscribe", "reply",
    "newsletter", "updates",
    "support+", "info+",
)


def parse_sender(from_header: str) -> dict:
    """
    Parse a From header into a dict with keys:
      email, firstname, lastname, company, domain
    Returns an empty dict if no valid email could be extracted.
    """
    display_name, email_addr = parseaddr(from_header)

    if not email_addr or "@" not in email_addr:
        return {}

    email_addr = email_addr.lower().strip()
    local, domain = email_addr.split("@", 1)
    domain = domain.lower()

    firstname, lastname = _split_name(display_name.strip())
    company = _domain_to_company(domain)

    return {
        "email": email_addr,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
    }


def is_automated_sender(email: str) -> bool:
    """Return True when the sender looks like a bot or mailing system."""
    local = email.split("@")[0].lower()
    return any(local.startswith(p) for p in _SKIP_PREFIXES)


# ── helpers ───────────────────────────────────────────────────────────────────

def _split_name(name: str) -> tuple:
    """Split a display name into (firstname, lastname)."""
    name = name.strip("\"'").strip()
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _domain_to_company(domain: str) -> str:
    """
    Derive a company name from an email domain.
    Returns an empty string for generic consumer providers.
    """
    if domain in _GENERIC_DOMAINS:
        return ""

    # Strip common subdomains (mail., m., webmail., …)
    parts = domain.split(".")
    # Heuristic: the meaningful name is usually the second-to-last label
    # e.g. mail.acme.com -> acme | acme.co.uk -> acme
    if len(parts) >= 2:
        return parts[-2].capitalize()

    return domain.capitalize()
