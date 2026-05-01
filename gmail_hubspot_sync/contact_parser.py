"""Parse sender information from Gmail From headers."""

import email.utils

FREE_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.it", "ymail.com",
    "hotmail.com", "hotmail.it", "outlook.com", "outlook.it", "live.com",
    "live.it", "icloud.com", "me.com", "mac.com", "aol.com",
    "protonmail.com", "proton.me", "tutanota.com", "tutamail.com",
    "libero.it", "virgilio.it", "tiscali.it", "alice.it", "fastwebnet.it",
    "tin.it", "inwind.it", "email.it",
}

SKIP_PATTERNS = ("noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon")


def parse_from_header(from_header: str) -> dict | None:
    """
    Return a contact dict extracted from a raw From header string, or None if
    the sender should be ignored (no-reply addresses, parse failures).
    """
    name, addr = email.utils.parseaddr(from_header)
    addr = addr.lower().strip()

    if not addr or "@" not in addr:
        return None

    if any(pat in addr for pat in SKIP_PATTERNS):
        return None

    first_name, last_name = _split_name(name.strip())
    domain = addr.split("@")[1]
    company = _company_from_domain(domain)

    return {
        "email": addr,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
    }


def _split_name(full_name: str) -> tuple[str | None, str | None]:
    if not full_name:
        return None, None
    parts = full_name.split(maxsplit=1)
    first = parts[0] if parts else None
    last = parts[1] if len(parts) > 1 else None
    return first, last


def _company_from_domain(domain: str) -> str | None:
    if domain in FREE_EMAIL_PROVIDERS:
        return None
    # Strip common TLD suffixes and capitalise: "acme-corp.it" → "Acme Corp"
    name_part = domain.rsplit(".", 1)[0]
    return name_part.replace("-", " ").replace("_", " ").title()
