import email.utils
from typing import Optional

# Common free-tier email domains — no company inference for these
_FREE_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "live.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com",
    "mail.com", "yandex.com", "gmx.com", "zoho.com", "fastmail.com",
    "tutanota.com", "inbox.com", "libero.it", "virgilio.it", "alice.it",
    "tiscali.it", "tin.it", "email.it", "yahoo.it", "hotmail.it",
    "msn.com", "pm.me", "hey.com",
}


def extract_contact(message: dict) -> Optional[dict]:
    """Return a contact dict parsed from a Gmail message metadata object."""
    headers = {
        h["name"]: h["value"]
        for h in message.get("payload", {}).get("headers", [])
    }

    from_header = headers.get("From", "").strip()
    if not from_header:
        return None

    name, email_addr = email.utils.parseaddr(from_header)
    email_addr = email_addr.lower().strip()

    if not email_addr or "@" not in email_addr:
        return None

    # Ignore no-reply / automated senders
    local_part = email_addr.split("@")[0]
    if any(kw in local_part for kw in ("noreply", "no-reply", "donotreply", "notifications", "mailer-daemon")):
        return None

    domain = email_addr.split("@")[1]
    firstname, lastname = _split_name(name.strip())
    company = _company_from_domain(domain)

    return {
        "email": email_addr,
        "firstname": firstname,
        "lastname": lastname,
        "company": company,
        "domain": domain,
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "message_id": message.get("id", ""),
    }


def _split_name(full_name: str) -> tuple[str, str]:
    if not full_name:
        return "", ""
    # Strip surrounding quotes some mail clients add
    full_name = full_name.strip('"\'')
    parts = full_name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_domain(domain: str) -> str:
    if domain in _FREE_DOMAINS:
        return ""
    # e.g. mail.acme.com → acme, acme.co.uk → acme
    parts = domain.split(".")
    # Skip generic mail subdomain prefixes
    base = parts[1] if parts[0] in ("mail", "email", "smtp", "mx") and len(parts) > 2 else parts[0]
    return base.capitalize()
