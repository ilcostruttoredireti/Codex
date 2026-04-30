"""Parse sender information from an email From header."""

import re
from email.utils import parseaddr

# Domains considered personal (no company derivation)
PERSONAL_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "yahoo.fr",
    "yahoo.co.uk", "hotmail.com", "hotmail.it", "hotmail.fr", "outlook.com",
    "live.com", "live.it", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "pm.me", "fastmail.com", "fastmail.fm",
    "zoho.com", "yandex.com", "yandex.ru", "gmx.com", "gmx.net",
    "gmx.de", "ymail.com", "libero.it", "virgilio.it", "tiscali.it",
    "alice.it", "tin.it", "email.it", "infinito.it",
})

# Local-part prefixes that indicate automated senders
_AUTOMATED_PREFIXES: frozenset[str] = frozenset({
    "noreply", "no-reply", "no_reply", "mailer-daemon", "postmaster",
    "bounce", "bounces", "do-not-reply", "donotreply", "daemon",
    "devnull", "null", "automailer", "auto-reply", "autoreply",
    "unsubscribe", "unsubscribe-reply",
})


def extract_contact(from_header: str) -> dict | None:
    """
    Parse a From header and return a contact dict, or None if the sender
    should be skipped (malformed, automated address, missing email).

    Returned dict keys:
        email, first_name, last_name, company, domain, is_personal
    """
    if not from_header:
        return None

    display_name, email_addr = parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    local_part, domain = email_addr.rsplit("@", 1)

    # Skip automated senders
    if local_part in _AUTOMATED_PREFIXES:
        return None

    # Split display name into first / last
    first_name, last_name = _split_name(display_name, local_part)

    # Derive company name from domain when it is not a personal domain
    is_personal = domain in PERSONAL_DOMAINS
    company = "" if is_personal else _company_from_domain(domain)

    return {
        "email": email_addr,
        "first_name": first_name,
        "last_name": last_name,
        "company": company,
        "domain": domain,
        "is_personal": is_personal,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_name(display_name: str, local_part: str) -> tuple[str, str]:
    """Return (first_name, last_name) from the display name or email local part."""
    name = display_name.strip().strip('"\'')
    if not name:
        # Fall back to local part: john.doe → John Doe
        name = re.sub(r"[._\-]+", " ", local_part).strip()

    parts = name.split(None, 1)
    first = parts[0].capitalize() if parts else ""
    last = parts[1].title() if len(parts) > 1 else ""
    return first, last


def _company_from_domain(domain: str) -> str:
    """
    Convert a domain to a human-readable company name.
    acme-corp.co.uk  →  Acme Corp
    """
    # Strip common TLDs (everything after the penultimate dot for known compound TLDs)
    base = domain.split(".")[0]
    return re.sub(r"[-_]+", " ", base).title()
