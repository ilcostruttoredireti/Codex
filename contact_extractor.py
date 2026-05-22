"""Parse sender contact details out of raw email headers."""

from email.utils import parseaddr

# Domains that do not carry company information.
_GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.it",
    "hotmail.com", "hotmail.it",
    "outlook.com", "outlook.it",
    "live.com", "live.it",
    "icloud.com", "me.com", "mac.com",
    "aol.com", "protonmail.com", "pm.me",
    "libero.it", "alice.it", "virgilio.it",
    "tiscali.it", "tin.it",
}


def extract_contact(headers: dict) -> dict | None:
    """Return a contact dict from email headers, or None if no usable sender.

    Returned keys: email, first_name, last_name, domain, company, subject.
    """
    raw_from = headers.get("From", "")
    display_name, email = parseaddr(raw_from)

    if not email or "@" not in email:
        return None

    email = email.lower().strip()
    domain = email.split("@", 1)[1]

    # Split display name into first / last
    first_name, last_name = "", ""
    name = display_name.strip()
    if name:
        parts = name.split(None, 1)  # split on first whitespace
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    # Derive company from domain when it is not a generic provider
    company = ""
    if domain not in _GENERIC_DOMAINS:
        # e.g. "acme.co.uk" → "Acme"
        company = domain.split(".")[0].capitalize()

    return {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "company": company,
        "subject": headers.get("Subject", ""),
    }
